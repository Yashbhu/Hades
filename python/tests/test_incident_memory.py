from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from agent_fakes import sample_report
from hades_agent.errors import (
    MemoryInputError,
    MemoryNotFoundError,
    MemorySchemaError,
    MemoryStoreError,
)
from hades_agent.incident_memory import (
    SCHEMA_VERSION,
    IncidentMemoryStore,
    cutoff_days_ago,
    metrics_from_incident,
)
from hades_agent.models import IncidentContext


def incident(
    incident_id: str,
    *,
    kind: str = "high_cpu",
    value: float = 90.0,
    at: str = "2026-01-01T00:00:00Z",
    state: str = "active",
    subject: str | None = None,
    summary: str | None = None,
) -> IncidentContext:
    evidence: dict[str, Any] = {"kind": kind}
    if kind == "high_cpu":
        evidence.update(
            {
                "observed": {"latest_percent": value},
                "derived": {"recent_average_percent": value},
            }
        )
    elif kind == "memory_pressure":
        evidence.update(
            {
                "observed": {
                    "latest_used_percent": value,
                    "latest_available_percent": 100.0 - value,
                },
                "derived": {"recent_average_available_percent": 100.0 - value},
            }
        )
    elif kind == "low_disk_space":
        evidence.update(
            {
                "observed": {
                    "mount_point": subject or "/",
                    "available_percent": 100.0 - value,
                }
            }
        )
    return IncidentContext(
        id=incident_id,
        kind=kind,
        severity="warning",
        state=state,
        subject=subject,
        summary=summary or f"{kind} observation {value}",
        started_at=at,
        updated_at=at,
        last_observed_at=at,
        evidence=evidence,
    )


class IncidentMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "incidents.sqlite"

    def seed_diagnosis(
        self,
        store: IncidentMemoryStore,
        incident_id: str,
        *,
        investigation_id: str,
        summary: str = "Prior diagnosis",
        evidence_summary: str = "Prior observation",
    ) -> None:
        store.record_investigation(
            investigation_id,
            f"run-{investigation_id}",
            incident_id=incident_id,
            question="prior question",
            model="local",
            stage="terminated",
        )
        store.save_outcome(
            investigation_id=investigation_id,
            execution_id=f"run-{investigation_id}",
            incident_id=incident_id,
            question="prior question",
            model="local",
            stage="terminated",
            termination_status="completed",
            report=sample_report(),
            evidence=[
                {
                    "evidence_id": f"ev-{investigation_id}",
                    "call_id": f"call-{investigation_id}",
                    "tool": "get_system_metrics",
                    "arguments": {},
                    "summary": evidence_summary,
                    "collected_at": "2026-01-01T00:00:01Z",
                }
            ],
        )

    def test_schema_is_versioned_and_idempotent(self) -> None:
        with IncidentMemoryStore(self.path) as store:
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
            self.assertEqual(store.list_incidents(), [])
            self.assertTrue(self.path.exists())
        with IncidentMemoryStore(self.path) as reopened:
            self.assertEqual(reopened.schema_version, SCHEMA_VERSION)

    def test_create_update_tags_and_filters(self) -> None:
        with IncidentMemoryStore(self.path) as store:
            created = store.upsert_incident(
                incident("inc-1", summary="first"), tags=("disk", "disk", "x")
            )
            self.assertEqual(created.tags, ("disk", "x"))
            store.upsert_incident(
                incident(
                    "inc-1",
                    value=96.0,
                    at="2026-01-02T00:00:00Z",
                    state="recovered",
                    summary="recovered now",
                )
            )
            record = store.get_incident_record("inc-1")
            self.assertEqual(record.summary, "recovered now")
            self.assertEqual(record.state, "recovered")
            self.assertEqual(record.resolution_status, "recovered")
            self.assertEqual(len(store.list_incidents()), 1)
            self.assertEqual(len(store.list_incidents(tag="disk")), 1)
            self.assertEqual(len(store.list_incidents(kind="high_cpu")), 1)
            self.assertEqual(len(store.list_incidents(kind="memory_pressure")), 0)
            detail = store.get_incident("inc-1")
            self.assertTrue(
                any(
                    event["event_type"] == "incident_created"
                    for event in detail["events"]
                )
            )

    def test_investigation_evidence_and_report_persist(self) -> None:
        with IncidentMemoryStore(self.path) as store:
            store.upsert_incident(incident("inc-1"))
            self.seed_diagnosis(store, "inc-1", investigation_id="inv-1")
            detail = store.get_incident("inc-1")
            self.assertEqual(len(detail["investigations"]), 1)
            self.assertEqual(len(detail["evidence"]), 1)
            self.assertEqual(detail["evidence"][0]["tool"], "get_system_metrics")
            self.assertEqual(len(detail["diagnoses"]), 1)
            self.assertEqual(detail["diagnoses"][0]["diagnosis_status"], "unverified")
            self.assertEqual(detail["diagnoses"][0]["supporting_evidence_json"], ["ev-inv-1"])
        with IncidentMemoryStore(self.path) as reopened:
            self.assertEqual(len(reopened.get_incident("inc-1")["evidence"]), 1)
            self.assertEqual(reopened.get_diagnosis("inv-1")["summary"], sample_report().summary)

    def test_verification_is_append_only_and_resolution_is_separate(self) -> None:
        with IncidentMemoryStore(self.path) as store:
            store.upsert_incident(incident("inc-1"))
            self.seed_diagnosis(store, "inc-1", investigation_id="inv-1")
            store.add_diagnosis_verification("inc-1", "confirmed", note="first")
            store.add_diagnosis_verification("inc-1", "disproved", note="second")
            detail = store.get_incident("inc-1")
            self.assertEqual(detail["diagnoses"][0]["diagnosis_status"], "disproved")
            self.assertEqual(len(detail["diagnosis_verifications"]), 2)
            self.assertEqual(
                {item["status"] for item in detail["diagnosis_verifications"]},
                {"confirmed", "disproved"},
            )
            resolved = store.resolve_incident("inc-1", "resolved", note="operator")
            self.assertEqual(resolved.resolution_status, "resolved")
            self.assertEqual(
                store.get_diagnosis("inv-1")["diagnosis_status"], "disproved"
            )
            # Persisting the same report again must not erase an explicit
            # verification outcome.
            self.seed_diagnosis(store, "inc-1", investigation_id="inv-1")
            self.assertEqual(
                store.get_diagnosis("inv-1")["diagnosis_status"], "disproved"
            )

    def test_verification_without_diagnosis_is_explicit(self) -> None:
        with IncidentMemoryStore(self.path) as store:
            store.upsert_incident(incident("inc-1"))
            with self.assertRaises(MemoryNotFoundError):
                store.add_diagnosis_verification("inc-1", "confirmed")

    def test_filtering_ranking_and_empty_history(self) -> None:
        with IncidentMemoryStore(self.path) as store:
            store.upsert_incident(
                incident("old-close", value=95.0, at="2026-01-01T00:00:00Z")
            )
            store.upsert_incident(
                incident("old-far", value=40.0, at="2026-01-02T00:00:00Z")
            )
            store.upsert_incident(
                incident(
                    "disk",
                    kind="low_disk_space",
                    value=95.0,
                    subject="/",
                    at="2026-01-03T00:00:00Z",
                )
            )
            found = store.find_relevant(
                incident("current", value=96.0, at="2026-01-04T00:00:00Z")
            )
            self.assertEqual(
                [item.incident.incident_id for item in found],
                ["old-close", "old-far"],
            )
            self.assertTrue(all(item.incident.kind == "high_cpu" for item in found))
            self.assertLess(
                found[0].mean_relative_delta or 1.0, found[1].mean_relative_delta or 1.0
            )
            self.assertIn("same incident type", found[0].ranking_reason)
            self.assertEqual(
                store.find_relevant(
                    incident("unknown", kind="memory_pressure", value=50.0)
                ),
                [],
            )
            self.assertEqual(
                [item.incident_id for item in store.list_incidents(limit=2)],
                ["disk", "old-far"],
            )

    def test_prior_diagnosis_is_labelled_historical(self) -> None:
        with IncidentMemoryStore(self.path) as store:
            store.upsert_incident(incident("inc-old", value=95.0))
            self.seed_diagnosis(store, "inc-old", investigation_id="inv-old")
            found = store.find_relevant(incident("inc-new", value=96.0))
            self.assertEqual(len(found), 1)
            data = found[0].to_dict()
            self.assertEqual(data["source"], "historical_incident_memory")
            self.assertFalse(data["is_current_evidence"])
            self.assertIsNotNone(data["prior_diagnosis"])
            self.assertFalse(data["prior_diagnosis"]["is_current_fact"])
            self.assertEqual(data["prior_diagnosis"]["investigation_id"], "inv-old")

    def test_prior_actions_are_retrieved_but_not_instructions(self) -> None:
        with IncidentMemoryStore(self.path) as store:
            store.upsert_incident(incident("inc-old", value=95.0))
            store.record_action(
                "inc-old",
                "Restart the noisy process",
                "executed",
                outcome="CPU returned to normal",
            )
            found = store.find_relevant(incident("inc-new", value=96.0))
            self.assertEqual(len(found), 1)
            actions = found[0].to_dict()["prior_actions"]
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0]["status"], "executed")
            self.assertEqual(actions[0]["source"], "historical_action")
            self.assertNotIn("instruction", actions[0])

    def test_metrics_are_extracted_and_sensitive_arguments_redacted(self) -> None:
        samples = metrics_from_incident(incident("inc-1", value=91.5))
        self.assertEqual({sample.name for sample in samples}, {
            "cpu_percent",
            "cpu_recent_average_percent",
        })
        with IncidentMemoryStore(self.path) as store:
            store.upsert_incident(incident("inc-1"))
            self.seed_diagnosis(
                store,
                "inc-1",
                investigation_id="inv-1",
                evidence_summary="x" * 10_000,
            )
            # Save one more evidence item with a sensitive top-level argument.
            store.save_outcome(
                investigation_id="inv-1",
                execution_id="run-inv-1",
                incident_id="inc-1",
                question="prior question",
                model="local",
                stage="terminated",
                termination_status="completed",
                report=sample_report(),
                evidence=[
                    {
                        "evidence_id": "ev-secret",
                        "call_id": "call-secret",
                        "tool": "get_recent_incidents",
                        "arguments": {"token": "top-secret", "limit": 2},
                        "summary": "safe",
                        "collected_at": "2026-01-01T00:00:02Z",
                    }
                ],
            )
            evidence = store.get_incident("inc-1")["evidence"]
            secret = next(item for item in evidence if item["call_id"] == "call-secret")
            self.assertEqual(secret["arguments_json"]["token"], "[redacted]")
            self.assertLessEqual(len(secret["summary"]), 4_096 + 100)

    def test_export_delete_and_prune(self) -> None:
        with IncidentMemoryStore(self.path) as store:
            store.upsert_incident(incident("old", at="2020-01-01T00:00:00Z"))
            self.seed_diagnosis(store, "old", investigation_id="inv-old")
            store.upsert_incident(incident("new", at="2030-01-01T00:00:00Z"))
            export = store.export()
            self.assertEqual(export["schema_version"], SCHEMA_VERSION)
            self.assertEqual(len(export["tables"]["incidents"]), 2)
            dry = store.prune("2025-01-01T00:00:00Z", dry_run=True)
            self.assertEqual(dry["incidents"], 1)
            self.assertTrue(dry["dry_run"])
            self.assertIsNotNone(store.get_incident_record("old"))
            pruned = store.prune("2025-01-01T00:00:00Z")
            self.assertEqual(pruned["incidents"], 1)
            with self.assertRaises(MemoryNotFoundError):
                store.get_incident_record("old")
            with self.assertRaises(MemoryNotFoundError):
                store.get_diagnosis("inv-old")
            store.delete_incident("new")
            self.assertEqual(store.list_incidents(), [])

    def test_corrupt_and_incompatible_databases_are_explicit(self) -> None:
        corrupt = Path(self.temp.name) / "corrupt.sqlite"
        corrupt.write_bytes(b"not sqlite")
        with self.assertRaises(MemoryStoreError):
            IncidentMemoryStore(corrupt)

        future = Path(self.temp.name) / "future.sqlite"
        connection = sqlite3.connect(future)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
        connection.close()
        with self.assertRaises(MemorySchemaError):
            IncidentMemoryStore(future)

    def test_concurrent_reads_and_writes(self) -> None:
        def write(index: int) -> str:
            with IncidentMemoryStore(self.path) as store:
                store.upsert_incident(
                    incident(
                        f"concurrent-{index}",
                        at=f"2026-01-{index + 1:02d}T00:00:00Z",
                    )
                )
                return store.list_incidents(limit=200)[0].incident_id

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write, range(8)))
        self.assertEqual(len(results), 8)
        with IncidentMemoryStore(self.path) as store:
            self.assertEqual(len(store.list_incidents(limit=200)), 8)

    def test_query_errors_are_translated(self) -> None:
        store = IncidentMemoryStore(self.path)
        store.close()
        with self.assertRaises(MemoryStoreError):
            store.list_incidents()

    def test_invalid_inputs_are_rejected(self) -> None:
        with IncidentMemoryStore(self.path) as store:
            with self.assertRaises(MemoryInputError):
                store.list_incidents(limit=0)
            with self.assertRaises(MemoryInputError):
                store.add_diagnosis_verification("missing", "maybe")
            with self.assertRaises(MemoryNotFoundError):
                store.resolve_incident("missing", "resolved")
            with self.assertRaises(MemoryInputError):
                cutoff_days_ago(0)
            with self.assertRaises(MemoryInputError):
                store.upsert_incident({"id": "bad"})


if __name__ == "__main__":
    unittest.main()
