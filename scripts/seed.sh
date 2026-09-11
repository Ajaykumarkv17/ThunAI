#!/usr/bin/env bash
# scripts/seed.sh — the ONE command a deployer runs after `cdk deploy --all`
# (tasks 21.x, not yet implemented) to populate the deployed account with the
# committed demo fixtures (Req 19.7, 8.1a; design.md §3.9).
#
# This is a thin bash entrypoint: the actual write logic (conditional/
# idempotent puts into `thunai-state`, S3 uploads, KB ingestion trigger) is
# implemented in Python (`scripts/seed_data.py`) so it can reuse
# `memory.state_store`'s Pydantic-validated, idempotent write helpers rather
# than reimplementing them in raw AWS CLI/jq.
#
# `scripts/` has no `__init__.py` yet (task 1.1's scaffolding did not make it
# an importable package), so this invokes `seed_data.py` as a direct script
# rather than via `python -m scripts.seed_data`.
#
# No interactive input (Req 19.7): every value this script/its Python helper
# needs is read from the environment (already expected to be set per
# `.env.example`'s convention), never prompted for.
#
# Idempotent (Req 19.7): every write inside `seed_data.py` is either a
# last-write-wins DynamoDB `put_item` (re-run = same state), an S3
# `PutObject` overwrite (re-uploading identical content is a no-op in
# effect), or a KB ingestion-job start guarded by a deterministic per-day
# idempotency token (see `seed_data.py`'s own verification note) — so running
# this script any number of times against the same deployed account
# converges to the same state every time, and is always safe to re-run after
# a partial failure.
#
# Scope explicitly NOT covered by this script (documented here so a future
# implementer of the referenced task knows where the responsibility lives):
#   - seed/hazard_readings/*.jsonl is read directly off disk by
#     SyntheticSensorProvider (task 10.1) via SENSOR_FIXTURE_PATH — never
#     uploaded anywhere by this script.
#   - seed/sample_messages/messages.json is POSTed to the real ingestion
#     endpoint DURING the demo by scripts/demo.sh (task 29.1, not yet
#     implemented) — never pre-loaded by this script.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Prefer an already-activated virtualenv's `python`; otherwise fall back to
# whatever `python3`/`python` is first on PATH. No venv is created or
# activated here — activation, like every other setup step, is assumed to
# already be done by the deployer (Req 19.7's "no interactive input" rules
# out this script prompting to create one).
if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "ERROR: seed.sh failed at step 'locate_python': no 'python' or 'python3' found on PATH." >&2
  exit 1
fi

exec "${PYTHON_BIN}" "${REPO_ROOT}/scripts/seed_data.py"
