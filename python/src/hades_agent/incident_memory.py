"""Local SQLite incident history and deterministic historical retrieval.

M6 keeps durable incident memory separate from M5's transient LangGraph
checkpoints. The database contains only the canonical M2 incident fields,
bounded diagnostic evidence, reports, and lifecycle metadata. Historical
records are returned as explicitly historical context; they are never merged
into current evidence.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .errors import (
    MemoryInputError,
    MemoryNotFoundError,
    MemorySchemaError,
    MemoryStoreError,
)
from .models import DiagnosticReport, IncidentContext

SCHEMA_VERSION = 1
MAX_HISTORY_CANDIDATES = 200
MAX_LIST_LIMIT = 200
MAX_EXPORT_LIMIT = 1_000
MAX_SUMMARY_CHARS = 4_096
MAX_TEXT_CHARS = 2_000
MAX_TAGS = 16
MAX_TAG_CHARS = 64
VALID_RESOLUTION_STATUSES = {"open", "resolved", "recovered"}
VALID_DIAGNOSIS_STATUSES = {"unknown", "unverified", "confirmed", "disproved"}
VALID_VERIFICATION_STATUSES = {"unknown", "confirmed", "disproved"}
VALID_ACTION_STATUSES = {"proposed", "approved", "executed", "rejected"}
SENSITIVE_ARGUMENT_KEYS = {
    "api_key",
    "authorization",
    "command",
    "cmd",
    "env",
    "environment",
    "password",
    "secret",
    "token",
}


def _translate_database_errors(method: Callable[..., Any]) -> Callable[..., Any]:
    """Turn raw SQLite failures into an actionable local-memory error."""

    @wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except sqlite3.Error as error:
            raise MemoryStoreError(
                f"incident-history database operation failed: {error}"
            ) from error

    return wrapper


@dataclass(frozen=True)
class MetricSample:
    """A bounded numerical observation attached to an incident."""

    name: str
    value: float
    unit: str = ""
    observed_at: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "unit": self.unit,
            "observed_at": self.observed_at,
            "source": self.source,
        }


@dataclass(frozen=True)
class IncidentRecord:
    """A durable incident summary without investigation state."""

    incident_id: str
    kind: str
    severity: str
    subject: str | None
    state: str
    summary: str
    started_at: str
    updated_at: str
    last_observed_at: str
    recovered_at: str | None
    resolution_status: str
    latest_diagnosis_status: str | None
    tags: tuple[str, ...]
    created_at: str
    record_updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "kind": self.kind,
            "severity": self.severity,
            "subject": self.subject,
            "state": self.state,
            "summary": self.summary,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "last_observed_at": self.last_observed_at,
            "recovered_at": self.recovered_at,
            "resolution_status": self.resolution_status,
            "latest_diagnosis_status": self.latest_diagnosis_status,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "record_updated_at": self.record_updated_at,
        }


@dataclass(frozen=True)
class HistoricalIncident:
    """A bounded, explicitly historical record supplied to the model."""

    incident: IncidentRecord
    prior_diagnosis: dict[str, Any] | None
    prior_actions: tuple[dict[str, Any], ...]
    metric_comparisons: tuple[dict[str, Any], ...]
    ranking_reason: str
    mean_relative_delta: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = self.incident.to_dict()
        data.update(
            {
                "source": "historical_incident_memory",
                "is_current_evidence": False,
                "prior_diagnosis": self.prior_diagnosis,
                "prior_actions": list(self.prior_actions),
                "metric_comparisons": list(self.metric_comparisons),
                "ranking_reason": self.ranking_reason,
                "mean_relative_delta": self.mean_relative_delta,
            }
        )
        return data


class IncidentMemoryStore:
    """Versioned SQLite store for incident history and retrieval."""

    def __init__(self, db_path: Path | str) -> None:
        raw_path = str(db_path)
        self.is_memory = raw_path == ":memory:"
        self._lock = threading.RLock()
        if self.is_memory:
            self.path = raw_path
            target = ":memory:"
        else:
            path = Path(raw_path).expanduser()
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            except OSError as error:
                raise MemoryStoreError(
                    f"cannot create incident-history directory {path.parent}: {error}"
                ) from error
            self.path = str(path)
            target = self.path
        try:
            self.conn = sqlite3.connect(target, check_same_thread=False, timeout=5)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON")
            self.conn.execute("PRAGMA busy_timeout = 5000")
            self.conn.commit()
            self._migrate()
            if not self.is_memory:
                self._enable_wal()
                self.conn.execute("PRAGMA synchronous = NORMAL")
                self.conn.commit()
        except MemoryStoreError:
            self._close_connection()
            raise
        except (sqlite3.DatabaseError, OSError) as error:
            self._close_connection()
            raise MemoryStoreError(
                f"cannot open incident-history database {self.path}: {error}"
            ) from error
        if not self.is_memory:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def _enable_wal(self) -> None:
        # Opening the same new database concurrently can race while SQLite is
        # switching journal modes. Retry briefly instead of failing the store.
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(6):
            try:
                self.conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower():
                    raise
                last_error = error
                if attempt < 5:
                    time.sleep(0.05 * (attempt + 1))
        if last_error is not None:
            raise last_error

    @property
    def schema_version(self) -> int:
        value = self.conn.execute("PRAGMA user_version").fetchone()[0]
        return int(value)

    def _close_connection(self) -> None:
        connection = getattr(self, "conn", None)
        if connection is None:
            return
        try:
            connection.close()
        except sqlite3.Error:
            pass

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> IncidentMemoryStore:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def _migrate(self) -> None:
        """Apply deterministic, transactional schema migrations."""
        with self._lock:
            try:
                current = self.schema_version
            except sqlite3.DatabaseError as error:
                raise MemorySchemaError(
                    f"cannot read incident-history schema version: {error}"
                ) from error
            if current > SCHEMA_VERSION:
                raise MemorySchemaError(
                    f"incident-history schema version {current} is newer than "
                    f"this build supports ({SCHEMA_VERSION}); export a backup with "
                    "a compatible build instead of modifying it"
                )
            if current == SCHEMA_VERSION:
                return
            try:
                with self._transaction():
                    current = self.schema_version
                    for target_version in range(current + 1, SCHEMA_VERSION + 1):
                        migration = getattr(
                            self, f"_apply_v{target_version}", None
                        )
                        if migration is None:
                            raise MemorySchemaError(
                                "no migration is registered for incident-history "
                                f"schema version {target_version}"
                            )
                        migration()
                        self.conn.execute(
                            f"PRAGMA user_version = {target_version}"
                        )
            except sqlite3.DatabaseError as error:
                raise MemorySchemaError(
                    f"incident-history schema migration failed: {error}"
                ) from error

    def _apply_v1(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                severity TEXT NOT NULL,
                subject TEXT,
                state TEXT NOT NULL,
                summary TEXT NOT NULL,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                recovered_at TEXT,
                resolution_status TEXT NOT NULL DEFAULT 'open'
                    CHECK (resolution_status IN ('open', 'resolved', 'recovered')),
                created_at TEXT NOT NULL,
                record_updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS incident_tags (
                incident_id TEXT NOT NULL
                    REFERENCES incidents(incident_id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                PRIMARY KEY (incident_id, tag)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS incident_metrics (
                metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL
                    REFERENCES incidents(incident_id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL DEFAULT '',
                observed_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'incident',
                UNIQUE (incident_id, name, source)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS investigations (
                investigation_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL,
                incident_id TEXT
                    REFERENCES incidents(incident_id) ON DELETE CASCADE,
                question TEXT NOT NULL,
                model TEXT NOT NULL,
                stage TEXT NOT NULL,
                termination_status TEXT,
                report_json TEXT,
                limits_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                incident_id TEXT
                    REFERENCES incidents(incident_id) ON DELETE CASCADE,
                investigation_id TEXT NOT NULL
                    REFERENCES investigations(investigation_id) ON DELETE CASCADE,
                tool TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                summary TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                call_id TEXT NOT NULL,
                truncated INTEGER NOT NULL DEFAULT 0,
                fresh_check INTEGER NOT NULL DEFAULT 0,
                duration_ms REAL NOT NULL DEFAULT 0,
                UNIQUE (investigation_id, call_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS diagnoses (
                diagnosis_id TEXT PRIMARY KEY,
                incident_id TEXT
                    REFERENCES incidents(incident_id) ON DELETE CASCADE,
                investigation_id TEXT NOT NULL UNIQUE
                    REFERENCES investigations(investigation_id) ON DELETE CASCADE,
                summary TEXT NOT NULL,
                report_json TEXT NOT NULL,
                confidence_level TEXT NOT NULL,
                confidence_explanation TEXT NOT NULL,
                diagnosis_status TEXT NOT NULL DEFAULT 'unverified'
                    CHECK (diagnosis_status IN
                        ('unknown', 'unverified', 'confirmed', 'disproved')),
                verification_note TEXT NOT NULL DEFAULT '',
                supporting_evidence_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS diagnosis_verifications (
                verification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                investigation_id TEXT NOT NULL
                    REFERENCES diagnoses(investigation_id) ON DELETE CASCADE,
                status TEXT NOT NULL
                    CHECK (status IN ('unknown', 'confirmed', 'disproved')),
                note TEXT NOT NULL DEFAULT '',
                verified_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS incident_actions (
                action_id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL
                    REFERENCES incidents(incident_id) ON DELETE CASCADE,
                investigation_id TEXT
                    REFERENCES investigations(investigation_id) ON DELETE CASCADE,
                action TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK (status IN ('proposed', 'approved', 'executed', 'rejected')),
                outcome TEXT NOT NULL DEFAULT '',
                proposed_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS incident_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE,
                incident_id TEXT NOT NULL
                    REFERENCES incidents(incident_id) ON DELETE CASCADE,
                investigation_id TEXT
                    REFERENCES investigations(investigation_id) ON DELETE CASCADE,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_incidents_kind_time "
            "ON incidents(kind, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_incidents_state_time "
            "ON incidents(state, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_incidents_resolution_time "
            "ON incidents(resolution_status, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_investigations_incident "
            "ON investigations(incident_id, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_evidence_incident "
            "ON evidence(incident_id, collected_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_evidence_investigation "
            "ON evidence(investigation_id)",
            "CREATE INDEX IF NOT EXISTS idx_metrics_name_time "
            "ON incident_metrics(name, observed_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_diagnoses_incident "
            "ON diagnoses(incident_id, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_events_incident "
            "ON incident_events(incident_id, occurred_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_tags_tag ON incident_tags(tag)",
        )
        for statement in statements:
            self.conn.execute(statement)

    @contextmanager
    def _transaction(self) -> Any:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except Exception:
                self.conn.rollback()
                raise
            else:
                self.conn.commit()

    # ------------------------------------------------------------------
    # Incident lifecycle
    # ------------------------------------------------------------------

    @_translate_database_errors
    def upsert_incident(
        self,
        incident: IncidentContext | Mapping[str, Any],
        *,
        tags: Sequence[str] = (),
        metrics: Sequence[MetricSample] | None = None,
        source: str = "bridge",
    ) -> IncidentRecord:
        """Create or refresh a canonical incident record idempotently."""
        payload = _incident_payload(incident)
        incident_id = _required_text(payload.get("id"), "incident.id", 200)
        now = _now()
        started_at = _canonical_timestamp(payload.get("started_at"), now)
        updated_at = _canonical_timestamp(payload.get("updated_at"), started_at)
        last_observed_at = _canonical_timestamp(
            payload.get("last_observed_at"), updated_at
        )
        recovered_at = _optional_timestamp(payload.get("recovered_at"))
        kind = _required_text(payload.get("kind"), "incident.kind", 100)
        severity = _required_text(payload.get("severity"), "incident.severity", 50)
        state = _required_text(payload.get("state"), "incident.state", 50)
        summary = _bounded_text(payload.get("summary"), "incident.summary", MAX_SUMMARY_CHARS)
        subject = _optional_text(payload.get("subject"), 500)
        normal_tags = _normalize_tags(tags)
        samples = tuple(metrics) if metrics is not None else metrics_from_incident(payload)
        with self._transaction():
            existing = self.conn.execute(
                "SELECT state, summary, resolution_status FROM incidents "
                "WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            resolution = "open"
            if state == "recovered":
                resolution = "recovered"
            self.conn.execute(
                """
                INSERT INTO incidents (
                    incident_id, kind, severity, subject, state, summary,
                    started_at, updated_at, last_observed_at, recovered_at,
                    resolution_status, created_at, record_updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    kind = excluded.kind,
                    severity = excluded.severity,
                    subject = excluded.subject,
                    state = excluded.state,
                    summary = excluded.summary,
                    started_at = CASE
                        WHEN excluded.started_at < incidents.started_at
                        THEN excluded.started_at ELSE incidents.started_at END,
                    updated_at = excluded.updated_at,
                    last_observed_at = excluded.last_observed_at,
                    recovered_at = COALESCE(
                        excluded.recovered_at, incidents.recovered_at
                    ),
                    resolution_status = CASE
                        WHEN incidents.resolution_status = 'open'
                             AND excluded.state = 'recovered'
                        THEN 'recovered' ELSE incidents.resolution_status END,
                    record_updated_at = excluded.record_updated_at
                """,
                (
                    incident_id,
                    kind,
                    severity,
                    subject,
                    state,
                    summary,
                    started_at,
                    updated_at,
                    last_observed_at,
                    recovered_at,
                    resolution,
                    now,
                    now,
                ),
            )
            for tag in normal_tags:
                self.conn.execute(
                    "INSERT OR IGNORE INTO incident_tags (incident_id, tag) "
                    "VALUES (?, ?)",
                    (incident_id, tag),
                )
            for sample in samples:
                self._insert_metric(
                    incident_id,
                    sample,
                    observed_at=_canonical_timestamp(
                        sample.observed_at or last_observed_at, last_observed_at
                    ),
                    source=_bounded_text(sample.source or source, "metric.source", 50),
                )
            if existing is None:
                self._add_event(
                    incident_id=incident_id,
                    investigation_id=None,
                    event_key=f"incident_created:{incident_id}",
                    event_type="incident_created",
                    detail=f"{kind} / {severity} / {state}",
                )
            elif existing["summary"] != summary or existing["state"] != state:
                self._add_event(
                    incident_id=incident_id,
                    investigation_id=None,
                    event_key=None,
                    event_type="incident_updated",
                    detail=f"{existing['state']} -> {state}",
                )
        return self._incident_record(incident_id)

    @_translate_database_errors
    def record_investigation(
        self,
        investigation_id: str,
        execution_id: str,
        *,
        incident_id: str | None,
        question: str,
        model: str,
        stage: str,
        limits: Mapping[str, Any] | None = None,
    ) -> None:
        """Associate an investigation with an incident, idempotently."""
        investigation_id = _required_text(
            investigation_id, "investigation_id", 128
        )
        execution_id = _required_text(execution_id, "execution_id", 200)
        question = _bounded_text(question, "question", 8_000)
        model = _bounded_text(model, "model", 200)
        stage = _bounded_text(stage, "stage", 100)
        if incident_id is not None:
            incident_id = _required_text(incident_id, "incident_id", 200)
        now = _now()
        with self._transaction():
            if incident_id is not None:
                self._require_incident(incident_id)
            existing = self.conn.execute(
                "SELECT created_at FROM investigations WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            self._upsert_investigation(
                investigation_id,
                execution_id,
                incident_id=incident_id,
                question=question,
                model=model,
                stage=stage,
                limits=limits,
                now=now,
            )
            if existing is None:
                self._add_event(
                    incident_id=incident_id,
                    investigation_id=investigation_id,
                    event_key=f"investigation_started:{investigation_id}",
                    event_type="investigation_started",
                    detail=question[:500],
                )

    @_translate_database_errors
    def save_outcome(
        self,
        *,
        investigation_id: str,
        execution_id: str,
        incident_id: str | None,
        question: str,
        model: str,
        stage: str,
        termination_status: str | None,
        report: DiagnosticReport | Mapping[str, Any] | None,
        evidence: Sequence[Mapping[str, Any]],
        limits: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist the final report and the current investigation evidence."""
        investigation_id = _required_text(
            investigation_id, "investigation_id", 128
        )
        execution_id = _required_text(execution_id, "execution_id", 200)
        question = _bounded_text(question, "question", 8_000)
        model = _bounded_text(model, "model", 200)
        stage = _bounded_text(stage, "stage", 100)
        if incident_id is not None:
            incident_id = _required_text(incident_id, "incident_id", 200)
        report_model = _coerce_report(report)
        report_json = report_model.model_dump(mode="json") if report_model else None
        safe_evidence = _normalize_evidence(evidence, investigation_id, incident_id)
        now = _now()
        with self._transaction():
            if incident_id is not None:
                self._require_incident(incident_id)
            existing = self.conn.execute(
                "SELECT created_at FROM investigations WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if existing is None:
                self._upsert_investigation(
                    investigation_id,
                    execution_id,
                    incident_id=incident_id,
                    question=question,
                    model=model,
                    stage=stage,
                    limits=limits,
                    now=now,
                )
            self.conn.execute(
                """
                UPDATE investigations SET
                    execution_id = ?,
                    incident_id = ?,
                    question = ?,
                    model = ?,
                    stage = ?,
                    termination_status = ?,
                    report_json = COALESCE(?, report_json),
                    limits_json = ?,
                    updated_at = ?
                WHERE investigation_id = ?
                """,
                (
                    execution_id,
                    incident_id,
                    question,
                    model,
                    stage,
                    termination_status,
                    json.dumps(report_json, ensure_ascii=False)
                    if report_json is not None
                    else None,
                    _json_dumps(limits or {}),
                    now,
                    investigation_id,
                ),
            )
            cited_tools = (
                {item.tool for item in report_model.evidence}
                if report_model is not None
                else set()
            )
            supporting_evidence_ids: list[str] = []
            for item in safe_evidence:
                self.conn.execute(
                    """
                    INSERT INTO evidence (
                        evidence_id, incident_id, investigation_id, tool,
                        arguments_json, summary, collected_at, call_id,
                        truncated, fresh_check, duration_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(investigation_id, call_id) DO UPDATE SET
                        incident_id = excluded.incident_id,
                        tool = excluded.tool,
                        arguments_json = excluded.arguments_json,
                        summary = excluded.summary,
                        collected_at = excluded.collected_at,
                        truncated = excluded.truncated,
                        fresh_check = excluded.fresh_check,
                        duration_ms = excluded.duration_ms
                    """,
                    (
                        item["evidence_id"],
                        incident_id,
                        investigation_id,
                        item["tool"],
                        item["arguments_json"],
                        item["summary"],
                        item["collected_at"],
                        item["call_id"],
                        int(item["truncated"]),
                        int(item["fresh_check"]),
                        item["duration_ms"],
                    ),
                )
                stored = self.conn.execute(
                    "SELECT evidence_id FROM evidence WHERE investigation_id = ? "
                    "AND call_id = ?",
                    (investigation_id, item["call_id"]),
                ).fetchone()
                if stored is not None:
                    stored_id = str(stored["evidence_id"])
                    if item["tool"] in cited_tools:
                        supporting_evidence_ids.append(stored_id)
            if report_model is not None:
                diagnosis_id = f"diag-{investigation_id}"
                self.conn.execute(
                    """
                    INSERT INTO diagnoses (
                        diagnosis_id, incident_id, investigation_id, summary,
                        report_json, confidence_level, confidence_explanation,
                        diagnosis_status, verification_note,
                        supporting_evidence_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'unverified', '', ?, ?, ?)
                    ON CONFLICT(investigation_id) DO UPDATE SET
                        incident_id = excluded.incident_id,
                        summary = excluded.summary,
                        report_json = excluded.report_json,
                        confidence_level = excluded.confidence_level,
                        confidence_explanation = excluded.confidence_explanation,
                        supporting_evidence_json = excluded.supporting_evidence_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        diagnosis_id,
                        incident_id,
                        investigation_id,
                        report_model.summary,
                        json.dumps(report_json, ensure_ascii=False),
                        report_model.confidence.level.value,
                        report_model.confidence.explanation,
                        json.dumps(supporting_evidence_ids, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                if incident_id is not None:
                    self._add_event(
                        incident_id=incident_id,
                        investigation_id=investigation_id,
                        event_key=f"report_saved:{investigation_id}",
                        event_type="report_saved",
                        detail=report_model.summary[:500],
                    )

    @_translate_database_errors
    def add_diagnosis_verification(
        self,
        incident_id: str,
        status: str,
        *,
        investigation_id: str | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """Append a verification event without overwriting prior outcomes."""
        incident_id = _required_text(incident_id, "incident_id", 200)
        if status not in VALID_VERIFICATION_STATUSES:
            raise MemoryInputError(
                "verification status must be unknown, confirmed, or disproved"
            )
        note = _bounded_text(note, "verification note", MAX_TEXT_CHARS)
        now = _now()
        with self._transaction():
            self._require_incident(incident_id)
            query = (
                "SELECT investigation_id FROM diagnoses WHERE incident_id = ? "
                "ORDER BY updated_at DESC, rowid DESC LIMIT 1"
            )
            params: tuple[Any, ...] = (incident_id,)
            if investigation_id is not None:
                investigation_id = _required_text(
                    investigation_id, "investigation_id", 128
                )
                query = (
                    "SELECT investigation_id FROM diagnoses "
                    "WHERE incident_id = ? AND investigation_id = ? LIMIT 1"
                )
                params = (incident_id, investigation_id)
            row = self.conn.execute(query, params).fetchone()
            if row is None:
                raise MemoryNotFoundError(
                    f"no diagnosis exists for incident {incident_id}"
                )
            diagnosis_investigation = str(row["investigation_id"])
            self.conn.execute(
                "INSERT INTO diagnosis_verifications "
                "(investigation_id, status, note, verified_at) VALUES (?, ?, ?, ?)",
                (diagnosis_investigation, status, note, now),
            )
            self.conn.execute(
                "UPDATE diagnoses SET diagnosis_status = ?, verification_note = ?, "
                "updated_at = ? WHERE investigation_id = ?",
                (status, note, now, diagnosis_investigation),
            )
            self._add_event(
                incident_id=incident_id,
                investigation_id=diagnosis_investigation,
                event_key=None,
                event_type="diagnosis_verification",
                detail=f"{status}: {note[:500]}",
            )
        return self.get_diagnosis(diagnosis_investigation)

    @_translate_database_errors
    def resolve_incident(
        self, incident_id: str, status: str, *, note: str = ""
    ) -> IncidentRecord:
        """Mark an incident resolved/recovered without confirming a diagnosis."""
        incident_id = _required_text(incident_id, "incident_id", 200)
        if status not in VALID_RESOLUTION_STATUSES:
            raise MemoryInputError(
                "resolution status must be open, resolved, or recovered"
            )
        note = _bounded_text(note, "resolution note", MAX_TEXT_CHARS)
        now = _now()
        with self._transaction():
            self._require_incident(incident_id)
            self.conn.execute(
                "UPDATE incidents SET resolution_status = ?, record_updated_at = ? "
                "WHERE incident_id = ?",
                (status, now, incident_id),
            )
            self._add_event(
                incident_id=incident_id,
                investigation_id=None,
                event_key=None,
                event_type="resolution_status_changed",
                detail=f"{status}: {note[:500]}",
            )
        return self._incident_record(incident_id)

    @_translate_database_errors
    def record_action(
        self,
        incident_id: str,
        action: str,
        status: str,
        *,
        investigation_id: str | None = None,
        outcome: str = "",
    ) -> dict[str, Any]:
        """Record a remediation proposal/outcome if a caller supplies one.

        M6 never executes remediation. This method only stores explicit,
        caller-supplied lifecycle facts.
        """
        incident_id = _required_text(incident_id, "incident_id", 200)
        if status not in VALID_ACTION_STATUSES:
            raise MemoryInputError(
                "action status must be proposed, approved, executed, or rejected"
            )
        action = _bounded_text(action, "action", MAX_TEXT_CHARS)
        outcome = _bounded_text(outcome, "action outcome", MAX_TEXT_CHARS)
        action_id = f"act-{uuid.uuid4().hex}"
        now = _now()
        with self._transaction():
            self._require_incident(incident_id)
            if investigation_id is not None:
                investigation_id = _required_text(
                    investigation_id, "investigation_id", 128
                )
            self.conn.execute(
                """
                INSERT INTO incident_actions (
                    action_id, incident_id, investigation_id, action, status,
                    outcome, proposed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action_id,
                    incident_id,
                    investigation_id,
                    action,
                    status,
                    outcome,
                    now,
                    now,
                ),
            )
            self._add_event(
                incident_id=incident_id,
                investigation_id=investigation_id,
                event_key=None,
                event_type="action_recorded",
                detail=f"{status}: {action[:500]}",
            )
        return {
            "action_id": action_id,
            "incident_id": incident_id,
            "investigation_id": investigation_id,
            "action": action,
            "status": status,
            "outcome": outcome,
            "proposed_at": now,
            "updated_at": now,
        }

    # ------------------------------------------------------------------
    # Reads and historical retrieval
    # ------------------------------------------------------------------

    @_translate_database_errors
    def list_incidents(
        self,
        *,
        limit: int = 20,
        kind: str | None = None,
        state: str | None = None,
        resolution_status: str | None = None,
        severity: str | None = None,
        tag: str | None = None,
        since: str | None = None,
    ) -> list[IncidentRecord]:
        """Return bounded, filtered incidents ordered by update time."""
        limit = _bounded_limit(limit, MAX_LIST_LIMIT)
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("i.kind = ?")
            params.append(_bounded_text(kind, "kind filter", 100))
        if state is not None:
            clauses.append("i.state = ?")
            params.append(_bounded_text(state, "state filter", 50))
        if resolution_status is not None:
            if resolution_status not in VALID_RESOLUTION_STATUSES:
                raise MemoryInputError("invalid resolution status filter")
            clauses.append("i.resolution_status = ?")
            params.append(resolution_status)
        if severity is not None:
            clauses.append("i.severity = ?")
            params.append(_bounded_text(severity, "severity filter", 50))
        if tag is not None:
            clauses.append(
                "EXISTS (SELECT 1 FROM incident_tags t "
                "WHERE t.incident_id = i.incident_id AND t.tag = ?)"
            )
            params.append(_bounded_text(tag, "tag filter", MAX_TAG_CHARS))
        if since is not None:
            clauses.append("i.updated_at >= ?")
            params.append(_canonical_timestamp(since, _now()))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT i.*,
                    (SELECT d.diagnosis_status FROM diagnoses d
                     WHERE d.incident_id = i.incident_id
                     ORDER BY d.updated_at DESC, d.rowid DESC LIMIT 1)
                    AS latest_diagnosis_status
                FROM incidents i
                """
                + where
                + " ORDER BY i.updated_at DESC, i.incident_id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [self._record_from_row(row) for row in rows]

    @_translate_database_errors
    def get_incident_record(self, incident_id: str) -> IncidentRecord:
        incident_id = _required_text(incident_id, "incident_id", 200)
        return self._incident_record(incident_id)

    @_translate_database_errors
    def get_incident(self, incident_id: str) -> dict[str, Any]:
        """Return one incident with bounded associations and history."""
        incident_id = _required_text(incident_id, "incident_id", 200)
        with self._lock:
            record = self._incident_record(incident_id)
            tags = [
                str(row[0])
                for row in self.conn.execute(
                    "SELECT tag FROM incident_tags WHERE incident_id = ? ORDER BY tag",
                    (incident_id,),
                ).fetchall()
            ]
            metrics = [
                dict(row)
                for row in self.conn.execute(
                    "SELECT name, value, unit, observed_at, source "
                    "FROM incident_metrics WHERE incident_id = ? "
                    "ORDER BY name, observed_at DESC",
                    (incident_id,),
                ).fetchall()
            ]
            investigations = [
                _decoded_row(row)
                for row in self.conn.execute(
                    "SELECT investigation_id, execution_id, question, model, stage, "
                    "termination_status, report_json, limits_json, created_at, "
                    "updated_at FROM investigations WHERE incident_id = ? "
                    "ORDER BY updated_at DESC LIMIT 100",
                    (incident_id,),
                ).fetchall()
            ]
            evidence = [
                _decoded_row(row)
                for row in self.conn.execute(
                    "SELECT evidence_id, investigation_id, tool, arguments_json, "
                    "summary, collected_at, call_id, truncated, fresh_check, "
                    "duration_ms FROM evidence WHERE incident_id = ? "
                    "ORDER BY collected_at DESC LIMIT 200",
                    (incident_id,),
                ).fetchall()
            ]
            diagnoses = [
                _decoded_row(row)
                for row in self.conn.execute(
                    "SELECT diagnosis_id, investigation_id, summary, report_json, "
                    "confidence_level, confidence_explanation, diagnosis_status, "
                    "verification_note, supporting_evidence_json, created_at, "
                    "updated_at FROM diagnoses WHERE incident_id = ? "
                    "ORDER BY updated_at DESC LIMIT 100",
                    (incident_id,),
                ).fetchall()
            ]
            verifications = [
                dict(row)
                for row in self.conn.execute(
                    "SELECT v.investigation_id, v.status, v.note, v.verified_at "
                    "FROM diagnosis_verifications v "
                    "JOIN diagnoses d ON d.investigation_id = v.investigation_id "
                    "WHERE d.incident_id = ? ORDER BY v.verified_at DESC "
                    "LIMIT 200",
                    (incident_id,),
                ).fetchall()
            ]
            actions = [
                dict(row)
                for row in self.conn.execute(
                    "SELECT action_id, investigation_id, action, status, outcome, "
                    "proposed_at, updated_at FROM incident_actions "
                    "WHERE incident_id = ? ORDER BY updated_at DESC LIMIT 100",
                    (incident_id,),
                ).fetchall()
            ]
            events = [
                dict(row)
                for row in self.conn.execute(
                    "SELECT event_type, investigation_id, occurred_at, detail "
                    "FROM incident_events WHERE incident_id = ? "
                    "ORDER BY occurred_at DESC, event_id DESC LIMIT 200",
                    (incident_id,),
                ).fetchall()
            ]
        data = record.to_dict()
        data["tags"] = tags
        data.update(
            {
                "metrics": metrics,
                "investigations": investigations,
                "evidence": evidence,
                "diagnoses": diagnoses,
                "diagnosis_verifications": verifications,
                "actions": actions,
                "events": events,
                "schema_version": self.schema_version,
            }
        )
        return data

    @_translate_database_errors
    def get_diagnosis(self, investigation_id: str) -> dict[str, Any]:
        investigation_id = _required_text(
            investigation_id, "investigation_id", 128
        )
        with self._lock:
            row = self.conn.execute(
                "SELECT diagnosis_id, incident_id, investigation_id, summary, "
                "report_json, confidence_level, confidence_explanation, "
                "diagnosis_status, verification_note, supporting_evidence_json, "
                "created_at, updated_at FROM diagnoses WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if row is None:
                raise MemoryNotFoundError(
                    f"no diagnosis exists for investigation {investigation_id}"
                )
            return _decoded_row(row)

    @_translate_database_errors
    def find_relevant(
        self,
        current: IncidentContext | Mapping[str, Any],
        *,
        limit: int = 5,
        exclude_incident_id: str | None = None,
    ) -> list[HistoricalIncident]:
        """Find previous same-kind incidents with transparent ranking.

        Ranking is deterministic: same subject first, then incidents with
        comparable metric samples, then lower mean relative metric difference,
        then recency. A prior diagnosis is included only as a labelled
        historical record.
        """
        payload = _incident_payload(current)
        kind = _required_text(payload.get("kind"), "incident.kind", 100)
        current_id = payload.get("id")
        if exclude_incident_id is None and isinstance(current_id, str):
            exclude_incident_id = current_id
        limit = _bounded_limit(limit, 50)
        exclude_incident_id = (
            _required_text(exclude_incident_id, "exclude_incident_id", 200)
            if exclude_incident_id
            else None
        )
        subject = _optional_text(payload.get("subject"), 500)
        current_metrics = {
            sample.name: sample.value
            for sample in metrics_from_incident(payload)
        }
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT i.*,
                    (SELECT d.diagnosis_status FROM diagnoses d
                     WHERE d.incident_id = i.incident_id
                     ORDER BY d.updated_at DESC, d.rowid DESC LIMIT 1)
                    AS latest_diagnosis_status
                FROM incidents i
                WHERE i.kind = ? AND (? IS NULL OR i.incident_id != ?)
                ORDER BY i.updated_at DESC, i.incident_id DESC
                LIMIT ?
                """,
                (kind, exclude_incident_id, exclude_incident_id, MAX_HISTORY_CANDIDATES),
            ).fetchall()
            candidates = [self._record_from_row(row) for row in rows]
            metrics_by_incident = self._metrics_for_incidents(
                [record.incident_id for record in candidates]
            )
        scored: list[tuple[int, int, float, float, HistoricalIncident]] = []
        for candidate in candidates:
            prior_metrics = metrics_by_incident.get(candidate.incident_id, {})
            comparisons = _compare_metrics(current_metrics, prior_metrics)
            mean_delta = (
                sum(item["relative_delta"] for item in comparisons) / len(comparisons)
                if comparisons
                else None
            )
            subject_match = int(
                subject is not None
                and candidate.subject is not None
                and subject == candidate.subject
            )
            prior_diagnosis = self._prior_diagnosis(candidate.incident_id)
            prior_actions = self._prior_actions(candidate.incident_id)
            reasons = ["same incident type"]
            if subject_match:
                reasons.append("same subject")
            if mean_delta is not None:
                reasons.append(f"mean relative metric difference {mean_delta:.3f}")
            else:
                reasons.append("no comparable metric samples")
            historical = HistoricalIncident(
                incident=candidate,
                prior_diagnosis=prior_diagnosis,
                prior_actions=prior_actions,
                metric_comparisons=tuple(comparisons),
                ranking_reason="; ".join(reasons),
                mean_relative_delta=mean_delta,
            )
            scored.append(
                (
                    subject_match,
                    1 if comparisons else 0,
                    mean_delta if mean_delta is not None else float("inf"),
                    -_timestamp_value(candidate.updated_at),
                    historical,
                )
            )
        scored.sort(
            key=lambda item: (
                -item[0],
                -item[1],
                item[2],
                item[3],
                item[4].incident.incident_id,
            )
        )
        return [item[4] for item in scored[:limit]]

    @_translate_database_errors
    def export(self, *, limit: int = MAX_EXPORT_LIMIT) -> dict[str, Any]:
        """Export bounded structured records without external telemetry."""
        limit = _bounded_limit(limit, MAX_EXPORT_LIMIT)
        tables = {
            "incidents": (
                "incident_id, kind, severity, subject, state, summary, started_at, "
                "updated_at, last_observed_at, recovered_at, resolution_status, "
                "created_at, record_updated_at"
            ),
            "incident_tags": "incident_id, tag",
            "incident_metrics": (
                "incident_id, name, value, unit, observed_at, source"
            ),
            "investigations": (
                "investigation_id, execution_id, incident_id, question, model, "
                "stage, termination_status, report_json, limits_json, created_at, "
                "updated_at"
            ),
            "evidence": (
                "evidence_id, incident_id, investigation_id, tool, arguments_json, "
                "summary, collected_at, call_id, truncated, fresh_check, duration_ms"
            ),
            "diagnoses": (
                "diagnosis_id, incident_id, investigation_id, summary, report_json, "
                "confidence_level, confidence_explanation, diagnosis_status, "
                "verification_note, supporting_evidence_json, created_at, updated_at"
            ),
            "diagnosis_verifications": (
                "investigation_id, status, note, verified_at"
            ),
            "incident_actions": (
                "action_id, incident_id, investigation_id, action, status, outcome, "
                "proposed_at, updated_at"
            ),
            "incident_events": (
                "incident_id, investigation_id, event_type, occurred_at, detail"
            ),
        }
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "exported_at": _now(),
            "tables": {},
        }
        with self._lock:
            for table, columns in tables.items():
                rows = self.conn.execute(
                    f"SELECT {columns} FROM {table} ORDER BY rowid DESC LIMIT ?",
                    (limit,),
                ).fetchall()
                data["tables"][table] = [dict(row) for row in rows]
        return data

    # ------------------------------------------------------------------
    # Retention and deletion
    # ------------------------------------------------------------------

    @_translate_database_errors
    def delete_incident(
        self, incident_id: str, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """Delete one incident and all dependent records in one transaction."""
        incident_id = _required_text(incident_id, "incident_id", 200)
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise MemoryNotFoundError(f"incident {incident_id} does not exist")
            if dry_run:
                return {"incident_id": incident_id, "deleted": False, "dry_run": True}
            with self._transaction():
                self.conn.execute(
                    "DELETE FROM incidents WHERE incident_id = ?", (incident_id,)
                )
        return {"incident_id": incident_id, "deleted": True, "dry_run": False}

    @_translate_database_errors
    def prune(self, before: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Delete incidents and investigations older than a cutoff."""
        cutoff = _canonical_timestamp(before, _now())
        with self._lock:
            incidents = self.conn.execute(
                "SELECT COUNT(*) FROM incidents WHERE updated_at < ?", (cutoff,)
            ).fetchone()[0]
            investigations = self.conn.execute(
                "SELECT COUNT(*) FROM investigations WHERE updated_at < ?",
                (cutoff,),
            ).fetchone()[0]
            result = {
                "before": cutoff,
                "incidents": int(incidents),
                "investigations": int(investigations),
                "dry_run": dry_run,
            }
            if dry_run:
                return result
            with self._transaction():
                self.conn.execute(
                    "DELETE FROM incidents WHERE updated_at < ?", (cutoff,)
                )
                self.conn.execute(
                    "DELETE FROM investigations WHERE updated_at < ?", (cutoff,)
                )
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _insert_metric(
        self,
        incident_id: str,
        sample: MetricSample,
        *,
        observed_at: str,
        source: str,
    ) -> None:
        if not math.isfinite(sample.value):
            return
        self.conn.execute(
            """
            INSERT INTO incident_metrics (
                incident_id, name, value, unit, observed_at, source
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(incident_id, name, source) DO UPDATE SET
                value = excluded.value,
                unit = excluded.unit,
                observed_at = excluded.observed_at
            """,
            (
                incident_id,
                _required_text(sample.name, "metric name", 100),
                float(sample.value),
                _bounded_text(sample.unit, "metric unit", 30),
                observed_at,
                source,
            ),
        )

    def _require_incident(self, incident_id: str) -> None:
        row = self.conn.execute(
            "SELECT 1 FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            raise MemoryNotFoundError(f"incident {incident_id} does not exist")

    def _incident_record(self, incident_id: str) -> IncidentRecord:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT i.*,
                    (SELECT d.diagnosis_status FROM diagnoses d
                     WHERE d.incident_id = i.incident_id
                     ORDER BY d.updated_at DESC, d.rowid DESC LIMIT 1)
                    AS latest_diagnosis_status
                FROM incidents i WHERE i.incident_id = ?
                """,
                (incident_id,),
            ).fetchone()
            if row is None:
                raise MemoryNotFoundError(f"incident {incident_id} does not exist")
            tags = tuple(
                str(item[0])
                for item in self.conn.execute(
                    "SELECT tag FROM incident_tags WHERE incident_id = ? ORDER BY tag",
                    (incident_id,),
                ).fetchall()
            )
        return self._record_from_row(row, tags=tags)

    def _record_from_row(
        self, row: sqlite3.Row, *, tags: tuple[str, ...] | None = None
    ) -> IncidentRecord:
        if tags is None:
            with self._lock:
                tags = tuple(
                    str(item[0])
                    for item in self.conn.execute(
                        "SELECT tag FROM incident_tags WHERE incident_id = ? "
                        "ORDER BY tag",
                        (row["incident_id"],),
                    ).fetchall()
                )
        return IncidentRecord(
            incident_id=str(row["incident_id"]),
            kind=str(row["kind"]),
            severity=str(row["severity"]),
            subject=row["subject"],
            state=str(row["state"]),
            summary=str(row["summary"]),
            started_at=str(row["started_at"]),
            updated_at=str(row["updated_at"]),
            last_observed_at=str(row["last_observed_at"]),
            recovered_at=row["recovered_at"],
            resolution_status=str(row["resolution_status"]),
            latest_diagnosis_status=row["latest_diagnosis_status"],
            tags=tags,
            created_at=str(row["created_at"]),
            record_updated_at=str(row["record_updated_at"]),
        )

    def _metrics_for_incidents(
        self, incident_ids: Sequence[str]
    ) -> dict[str, dict[str, float]]:
        if not incident_ids:
            return {}
        placeholders = ",".join("?" for _ in incident_ids)
        rows = self.conn.execute(
            f"SELECT incident_id, name, value FROM incident_metrics "
            f"WHERE incident_id IN ({placeholders}) ORDER BY observed_at DESC",
            tuple(incident_ids),
        ).fetchall()
        result: dict[str, dict[str, float]] = {item: {} for item in incident_ids}
        for row in rows:
            incident_id = str(row["incident_id"])
            name = str(row["name"])
            if name not in result[incident_id]:
                result[incident_id][name] = float(row["value"])
        return result

    def _prior_diagnosis(self, incident_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT investigation_id, summary, report_json, confidence_level,
                       diagnosis_status, verification_note, updated_at
                FROM diagnoses WHERE incident_id = ?
                ORDER BY updated_at DESC, rowid DESC LIMIT 1
                """,
                (incident_id,),
            ).fetchone()
        if row is None:
            return None
        likely_causes: list[str] = []
        try:
            report = json.loads(row["report_json"])
        except (json.JSONDecodeError, TypeError):
            report = None
        if isinstance(report, Mapping):
            causes = report.get("likely_causes")
            if isinstance(causes, list):
                for cause in causes[:3]:
                    if isinstance(cause, Mapping) and isinstance(cause.get("cause"), str):
                        likely_causes.append(cause["cause"][:500])
        return {
            "investigation_id": str(row["investigation_id"]),
            "summary": str(row["summary"])[:1_000],
            "confidence_level": str(row["confidence_level"]),
            "diagnosis_status": str(row["diagnosis_status"]),
            "verification_note": str(row["verification_note"])[:1_000],
            "likely_causes": likely_causes,
            "updated_at": str(row["updated_at"]),
            "source": "historical_diagnosis",
            "is_current_fact": False,
        }

    def _upsert_investigation(
        self,
        investigation_id: str,
        execution_id: str,
        *,
        incident_id: str | None,
        question: str,
        model: str,
        stage: str,
        limits: Mapping[str, Any] | None,
        now: str,
    ) -> None:
        existing = self.conn.execute(
            "SELECT created_at FROM investigations WHERE investigation_id = ?",
            (investigation_id,),
        ).fetchone()
        created_at = existing["created_at"] if existing is not None else now
        self.conn.execute(
            """
            INSERT INTO investigations (
                investigation_id, execution_id, incident_id, question,
                model, stage, termination_status, report_json,
                limits_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
            ON CONFLICT(investigation_id) DO UPDATE SET
                execution_id = excluded.execution_id,
                incident_id = excluded.incident_id,
                question = excluded.question,
                model = excluded.model,
                stage = excluded.stage,
                limits_json = excluded.limits_json,
                updated_at = excluded.updated_at
            """,
            (
                investigation_id,
                execution_id,
                incident_id,
                question,
                model,
                stage,
                _json_dumps(limits or {}),
                created_at,
                now,
            ),
        )

    def _prior_actions(self, incident_id: str) -> tuple[dict[str, Any], ...]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT action_id, investigation_id, action, status, outcome, "
                "proposed_at, updated_at FROM incident_actions "
                "WHERE incident_id = ? ORDER BY updated_at DESC LIMIT 5",
                (incident_id,),
            ).fetchall()
        return tuple(
            {
                "action_id": str(row["action_id"]),
                "investigation_id": row["investigation_id"],
                "action": str(row["action"])[:500],
                "status": str(row["status"]),
                "outcome": str(row["outcome"])[:1_000],
                "proposed_at": str(row["proposed_at"]),
                "updated_at": str(row["updated_at"]),
                "source": "historical_action",
            }
            for row in rows
        )

    def _add_event(
        self,
        *,
        incident_id: str | None,
        investigation_id: str | None,
        event_key: str | None,
        event_type: str,
        detail: str,
    ) -> None:
        if incident_id is None:
            return
        self.conn.execute(
            """
            INSERT OR IGNORE INTO incident_events (
                event_key, incident_id, investigation_id, event_type,
                occurred_at, detail
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_key,
                incident_id,
                investigation_id,
                event_type,
                _now(),
                _bounded_text(detail, "event detail", MAX_TEXT_CHARS),
            ),
        )


def metrics_from_incident(
    incident: IncidentContext | Mapping[str, Any],
) -> tuple[MetricSample, ...]:
    """Extract only allowlisted numerical incident evidence.

    The M2 incident schema contains thresholds and measurements. Only current
    measurements are retained for comparison, never full environment data.
    """
    payload = _incident_payload(incident)
    evidence = payload.get("evidence")
    if not isinstance(evidence, Mapping):
        return ()
    kind = str(payload.get("kind", ""))
    observed = evidence.get("observed")
    derived = evidence.get("derived")
    values = observed if isinstance(observed, Mapping) else {}
    derived_values = derived if isinstance(derived, Mapping) else {}
    selected: list[tuple[str, Any, str]] = []
    if kind == "high_cpu":
        selected = [
            ("cpu_percent", values.get("latest_percent"), "%"),
            ("cpu_recent_average_percent", derived_values.get("recent_average_percent"), "%"),
        ]
    elif kind == "memory_pressure":
        selected = [
            ("memory_used_percent", values.get("latest_used_percent"), "%"),
            (
                "memory_available_percent",
                values.get("latest_available_percent"),
                "%",
            ),
            (
                "memory_recent_average_available_percent",
                derived_values.get("recent_average_available_percent"),
                "%",
            ),
        ]
    elif kind == "low_disk_space":
        selected = [
            ("disk_available_percent", values.get("available_percent"), "%"),
        ]
    else:
        # Accept only explicitly allowlisted flat percentages for future kinds.
        for key in (
            "cpu_percent",
            "memory_used_percent",
            "memory_available_percent",
            "disk_available_percent",
        ):
            if key in evidence:
                selected.append((key, evidence.get(key), "%"))
    result: list[MetricSample] = []
    for name, value, unit in selected:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        number = float(value)
        if not math.isfinite(number):
            continue
        result.append(MetricSample(name=name, value=number, unit=unit))
    return tuple(result)


def cutoff_days_ago(days: int) -> str:
    """Return a canonical UTC cutoff for retention commands."""
    if not isinstance(days, int) or days < 1:
        raise MemoryInputError("retention days must be a positive integer")
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="microseconds"
    )


def _incident_payload(
    incident: IncidentContext | Mapping[str, Any]
) -> dict[str, Any]:
    if isinstance(incident, IncidentContext):
        return incident.model_dump(mode="json")
    if not isinstance(incident, Mapping):
        raise MemoryInputError("incident must be an object")
    try:
        return IncidentContext.model_validate(dict(incident)).model_dump(mode="json")
    except ValidationError as error:
        raise MemoryInputError(f"invalid incident: {error}") from error


def _coerce_report(
    report: DiagnosticReport | Mapping[str, Any] | None,
) -> DiagnosticReport | None:
    if report is None:
        return None
    if isinstance(report, DiagnosticReport):
        return report
    if isinstance(report, Mapping):
        try:
            return DiagnosticReport.model_validate(dict(report))
        except ValidationError as error:
            raise MemoryInputError(f"invalid diagnostic report: {error}") from error
    raise MemoryInputError("diagnostic report must be an object")


def _normalize_evidence(
    evidence: Sequence[Mapping[str, Any]],
    investigation_id: str,
    incident_id: str | None,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        call_id = _required_text(
            item.get("call_id") or f"call-{len(normalized) + 1}",
            "evidence.call_id",
            300,
        )
        if call_id in seen:
            continue
        seen.add(call_id)
        normalized.append(
            {
                "evidence_id": _required_text(
                    item.get("evidence_id") or f"ev-{uuid.uuid4().hex}",
                    "evidence_id",
                    200,
                ),
                "investigation_id": investigation_id,
                "incident_id": incident_id,
                "tool": _required_text(item.get("tool"), "evidence.tool", 100),
                "arguments_json": _json_dumps(_safe_arguments(item.get("arguments", {}))),
                "summary": _bounded_text(
                    item.get("summary", ""), "evidence.summary", MAX_SUMMARY_CHARS
                ),
                "collected_at": _canonical_timestamp(
                    item.get("collected_at"), _now()
                ),
                "call_id": call_id,
                "truncated": bool(item.get("truncated", False)),
                "fresh_check": bool(item.get("fresh_check", False)),
                "duration_ms": _bounded_float(item.get("duration_ms", 0.0)),
            }
        )
    return normalized


def _bounded_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else 0.0


def _compare_metrics(
    current: Mapping[str, float], prior: Mapping[str, float]
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for name in sorted(set(current) & set(prior)):
        current_value = current[name]
        prior_value = prior[name]
        delta = prior_value - current_value
        denominator = max(abs(current_value), abs(prior_value), 1e-9)
        comparisons.append(
            {
                "name": name,
                "current": round(current_value, 6),
                "prior": round(prior_value, 6),
                "delta": round(delta, 6),
                "relative_delta": round(abs(delta) / denominator, 6),
            }
        )
    return comparisons


def _safe_arguments(arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for key, value in arguments.items():
        key_text = str(key)
        if key_text.lower() in SENSITIVE_ARGUMENT_KEYS:
            safe[key_text] = "[redacted]"
        else:
            safe[key_text] = value
    return safe


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _decoded_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for field in (
        "arguments_json",
        "report_json",
        "limits_json",
        "supporting_evidence_json",
    ):
        if field not in data or data[field] is None:
            continue
        try:
            data[field] = json.loads(data[field])
        except (json.JSONDecodeError, TypeError):
            pass
    for field in ("truncated", "fresh_check"):
        if field in data:
            data[field] = bool(data[field])
    return data


def _required_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise MemoryInputError(f"{field} must be a string")
    text = value.strip()
    if not text:
        raise MemoryInputError(f"{field} must not be empty")
    if len(text) > limit:
        raise MemoryInputError(f"{field} is longer than {limit} characters")
    return text


def _bounded_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise MemoryInputError(f"{field} must be a string")
    if len(value) > limit:
        marker = "\n[truncated by Hades]"
        if limit <= len(marker):
            return value[:limit]
        return value[: limit - len(marker)] + marker
    return value


def _optional_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise MemoryInputError("optional text field must be a string or null")
    return value[:limit]


def _normalize_tags(tags: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for tag in tags:
        if not isinstance(tag, str):
            continue
        normalized = tag.strip()
        if not normalized:
            continue
        result.append(normalized[:MAX_TAG_CHARS])
        if len(result) >= MAX_TAGS:
            break
    return tuple(dict.fromkeys(result))


def _bounded_limit(value: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MemoryInputError("limit must be an integer")
    if value < 1:
        raise MemoryInputError("limit must be at least 1")
    return min(value, maximum)


def _canonical_timestamp(value: Any, default: str) -> str:
    if value is None or value == "":
        return default
    if not isinstance(value, str):
        raise MemoryInputError("timestamp must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MemoryInputError(f"invalid timestamp {value!r}") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _optional_timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _canonical_timestamp(value, _now())


def _timestamp_value(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
