# Hades evaluation harness

This directory contains a separate evaluation harness. It does not change Hades
production behavior, defaults, or monitoring logic.

## Paths compared

- **Deterministic:** collects the same read-only tools and produces a template
  report without invoking an LLM.
- **LLM:** uses the existing `InvestigationWorkflow` and configured Ollama
  model without changing its behavior.

## Quality evaluation

Fake backend (harness correctness only):

```sh
PYTHONPATH=python/src:python/tests:python/eval python/.venv/bin/python \\
  python/eval/hades_eval/run_quality.py --backend fake --history \\
  --output python/eval/results/quality_fake.json
```

Live backend (requires Rust service and Ollama):

```sh
./target/debug/os-agent serve --bind 127.0.0.1:8765
PYTHONPATH=python/src:python/tests:python/eval python/.venv/bin/python \\
  python/eval/hades_eval/run_quality.py --backend live --history \\
  --output python/eval/results/quality_live.json
```

## Overhead evaluation

```sh
PYTHONPATH=python/src:python/tests:python/eval python/.venv/bin/python \\
  python/eval/hades_eval/run_overhead.py --trials 3 --duration 3 --iterations 5 \\
  --output python/eval/results/overhead_local.json
```

## Scoring rubric

- `detection_correct`: expected incident kind matches the scenario incident kind.
- `citation_valid`: every report evidence tool is a successful tool call, and
  every `supporting_evidence` index is valid.
- `unsupported_claim_count`: hypotheses with no valid supporting evidence.
- `contradiction_count`: obvious contradiction between report wording and the
  scenario symptom.
- `useful_beyond_deterministic`: automated, conservative flag based on valid
  citations plus next checks or data gaps. Final usefulness requires human
  review.

A valid citation is not proof that the associated claim is factually supported.

## Limitations

- Fake-backend results validate harness plumbing only.
- Live LLM evaluation requires Ollama and a tool-calling model; if unavailable,
  the harness records a skip instead of fabricating results.
- Scenarios are synthetic and use bounded point-in-time snapshots.
- Temperature and power are not measured in this environment.
- Detection evaluation relies on the existing Rust unit tests; this harness does
  not inject synthetic snapshots into the Rust monitor.
- Diagnostic quality is not the same as real-world usefulness.
