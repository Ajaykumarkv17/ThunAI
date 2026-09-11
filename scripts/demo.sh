#!/usr/bin/env bash
# scripts/demo.sh - the ONE command that runs ThunAI's judged demo loop
# end to end, locally, with NO third-party credentials (Req 19.12; design 3.9).
#
# What it exercises: the full trigger -> autonomous work -> one escalation ->
# resume -> audit loop, replayed by the recorded eval suite (python -m evals)
# against in-process doubles of every external interface. SYNTHETIC_SENSORS=1
# means the seeded rising-river sweep is driven from the committed hazard
# fixtures, and no live AWS call is made - so a clean clone runs this offline.
#
# Contract (Req 19.12):
#   * one command, no interactive input;
#   * completes within DEMO_TIMEOUT_SECONDS (default 600s / 10 min);
#   * asserts the single-escalation-per-sweep property (Req 11.9) via evals;
#   * exits non-zero naming the FIRST incomplete step on failure.
#
# Usage:
#   ./scripts/demo.sh
#   DEMO_TIMEOUT_SECONDS=300 ./scripts/demo.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

DEMO_TIMEOUT_SECONDS="${DEMO_TIMEOUT_SECONDS:-600}"

# Credential-free by construction: force the synthetic sensor backend and
# provide placeholder model ids so validate_config() passes with zero exports.
export SYNTHETIC_SENSORS="${SYNTHETIC_SENSORS:-1}"
export AWS_REGION="${AWS_REGION:-us-west-2}"
export THUNAI_LOW_COST_MODEL_ID="${THUNAI_LOW_COST_MODEL_ID:-us.amazon.nova-lite-v1:0}"
export THUNAI_HIGH_CAPABILITY_MODEL_ID="${THUNAI_HIGH_CAPABILITY_MODEL_ID:-us.amazon.nova-pro-v1:0}"
export SENSOR_FIXTURE_PATH="${SENSOR_FIXTURE_PATH:-seed/hazard_readings}"

CURRENT_STEP="locate_python"
if command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
else
  echo "ERROR: demo.sh failed at step '${CURRENT_STEP}': no 'python' or 'python3' on PATH." >&2
  exit 1
fi

fail() {
  echo "ERROR: demo.sh failed at step '${CURRENT_STEP}'." >&2
  echo "       ${1}" >&2
  exit 1
}

run_bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "${DEMO_TIMEOUT_SECONDS}" "$@"
  else
    "$@"
  fi
}

echo "== ThunAI demo loop =========================================="
echo "   repo:             ${REPO_ROOT}"
echo "   python:           $(${PYTHON_BIN} --version 2>&1)"
echo "   SYNTHETIC_SENSORS=${SYNTHETIC_SENSORS}  (credential-free)"
echo "   time budget:      ${DEMO_TIMEOUT_SECONDS}s"
echo "=============================================================="

CURRENT_STEP="verify_dependencies"
"${PYTHON_BIN}" - <<'PY' || fail "install deps first:  pip install -e .[test]"
import importlib.util, sys
missing = [m for m in ("strands", "pydantic", "bedrock_agentcore") if not importlib.util.find_spec(m)]
if missing:
    print("missing modules: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
PY

CURRENT_STEP="validate_cold_start"
"${PYTHON_BIN}" - <<'PY' || fail "cold-start validation reported problems (see above)."
import sys
from surface.entrypoint import validate_cold_start
problems = validate_cold_start(force=True)
if problems:
    print("cold-start problems:", *problems, sep="\n  - ", file=sys.stderr)
    sys.exit(1)
print("cold-start validation OK")
PY

CURRENT_STEP="run_eval_loop"
run_bounded "${PYTHON_BIN}" -m evals \
  || fail "the demo loop did not pass (a scenario failed, or it exceeded ${DEMO_TIMEOUT_SECONDS}s)."

echo "=============================================================="
echo "ThunAI demo loop PASSED - trigger -> autonomous work -> one"
echo "escalation -> resume -> audit completed on the seeded fixtures."
echo "Per-scenario results: evals/results/latest.json"
echo "=============================================================="