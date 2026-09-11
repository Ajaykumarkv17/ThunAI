"""ThunAI Strands Evals scenario suite (Req 21.1, 21.2, 21.11-21.13, design.md Testing Strategy).

The eval suite records ``20+`` scenarios (``evals/scenarios/``) that are replayed
against the *real* ThunAI decision logic — the deterministic ``Rule_Engine``
severity banding (``agents.rule_engine.evaluate_rule_engine``) and the single
autonomy authority (``policy.escalation_policy.decide``) — using **deterministic
evaluators** (exact match on structured outputs, trajectory checks on which
tools/decisions were exercised, and environment-state assertions) rather than
an LLM judge, exactly as the design's Testing Strategy prescribes.

Every external interface is replaced by an in-process double (the seeded
fixtures under ``seed/``), so a run needs **zero** third-party credentials and
makes **no** live AWS call — this is a CI-only regression mechanism, distinct
from the deployed system (Req 21.10).

Public surface:
    - ``harness``   — scenario/result dataclasses, the scenario loader, the
      deterministic evaluators, and ``run_suite``.
    - ``Eval_Suite`` version constant ``EVAL_SUITE_VERSION``.
"""

from __future__ import annotations

from evals.harness import (
    EVAL_SUITE_VERSION,
    ScenarioResult,
    SuiteResult,
    compute_goal_success,
    load_scenarios,
    run_scenario,
    run_suite,
    write_results,
)

__all__ = [
    "EVAL_SUITE_VERSION",
    "ScenarioResult",
    "SuiteResult",
    "compute_goal_success",
    "load_scenarios",
    "run_scenario",
    "run_suite",
    "write_results",
]
