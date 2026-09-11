"""Model routing and cold-start configuration validation (Req 19.9, 20.6).

This is **the ONE module holding model ids** (design.md's Cost Model
section, `# agents/config.py -- the ONE module holding model ids
(Req 19.9)`): every specialist agent (tasks 13.2-13.8) and
`Coordinator_Orchestrator` (task 13.8) must obtain its Bedrock model id by
calling `get_model_id_for_role()` from this module rather than hardcoding a
model id string, so that a single env change re-routes every agent.

Verification note (mandatory per tasks.md 13.1, "verify first"):
    - Installed version confirmed: `strands-agents==1.55.0` (`pip show
      strands-agents`, matching `pyproject.toml`'s pin). Checked the current
      Strands Agents docs (`amazon-bedrock.md`, "Configuration Options" /
      "Custom Configuration") for `strands.models.BedrockModel`'s
      constructor: `model_id` (the Bedrock model identifier string),
      `region_name` (AWS region override), `temperature` (sampling
      randomness), and `max_tokens` are all still current, documented
      constructor parameters — no deviation from design.md's assumed
      `BedrockModel(model_id=..., region_name=..., temperature=...,
      max_tokens=...)` shape. This module deliberately does **not**
      construct `BedrockModel` instances itself — it only resolves *which*
      model id string a role should use — so it takes no direct dependency
      on the `BedrockModel` constructor; that dependency belongs to each
      specialist agent module (tasks 13.2-13.8), which import
      `get_model_id_for_role()` from here and pass the returned id into
      their own `BedrockModel(model_id=..., ...)` construction.
    - Confirmed the current Amazon Bedrock model-card pages for the exact
      Nova model id strings referenced by `.env.example`'s guidance ("Use
      the Amazon Nova family: a Nova Lite-class id for low-cost extraction/
      classification, a Nova Pro-class id for trade-off reasoning/
      orchestration"): `us.amazon.nova-lite-v1:0` (Nova Lite model card,
      "Programmatic Access" section) and `us.amazon.nova-pro-v1:0` (Nova Pro
      model card, "Programmatic Access" section). Both are available as
      in-region inference in `us-west-2`, the region already defaulted in
      `.env.example`'s `AWS_REGION`. This module does not hardcode either
      id — they are supplied via `THUNAI_LOW_COST_MODEL_ID` /
      `THUNAI_HIGH_CAPABILITY_MODEL_ID` per design.md, and the ids above are
      recorded here only as the values a deployer would put in `.env` (also
      noted in `.env.example`).

Role -> tier mapping judgment call:
    design.md's Cost Model section explicitly names all seven roles' tier
    assignment (the code block under "Model routing (Req 20.6)"), so
    `MODEL_FOR_ROLE` below is transcribed directly from design.md rather
    than independently judged: `monitor_agent`, `dispatch_agent`, and
    `coordinator_orchestrator` route to `HIGH_CAPABILITY_MODEL_ID` (each
    involves trade-off reasoning or ad-hoc human-facing routing); the
    remaining four roles (`intake_agent`, `alert_agent`, `safety_qa_agent`,
    `knowledge_agent`) route to `LOW_COST_MODEL_ID` (each is
    extraction/classification, templated composition, fixed-rubric policy
    checking, or grounded retrieval+summarisation — design.md's "boring
    hop" candidates). No independent judgment call was required.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

# ---------------------------------------------------------------------------
# Two env-driven model ids (Req 19.9, 20.6). Read once at import time so a
# missing value fails loudly and early rather than deep inside an agent
# invocation; `validate_config()` re-checks these before any run starts and
# names every problem rather than relying solely on this import-time read.
# ---------------------------------------------------------------------------
LOW_COST_MODEL_ID: Final[str] = os.environ.get("THUNAI_LOW_COST_MODEL_ID", "")
"""Nova Lite-class model id for low-cost extraction/classification roles."""

HIGH_CAPABILITY_MODEL_ID: Final[str] = os.environ.get(
    "THUNAI_HIGH_CAPABILITY_MODEL_ID", ""
)
"""Nova Pro-class model id for trade-off reasoning/orchestration roles."""

# ---------------------------------------------------------------------------
# The seven agent roles named in design.md §3.1 / tasks 13.2-13.8, mapped to
# the model id each should use (design.md's Cost Model code block).
# ---------------------------------------------------------------------------
AGENT_ROLES: Final[frozenset[str]] = frozenset(
    {
        "monitor_agent",
        "intake_agent",
        "dispatch_agent",
        "alert_agent",
        "safety_qa_agent",
        "knowledge_agent",
        "coordinator_orchestrator",
    }
)

MODEL_FOR_ROLE: Final[dict[str, str]] = {
    "monitor_agent": HIGH_CAPABILITY_MODEL_ID,
    "intake_agent": LOW_COST_MODEL_ID,
    "dispatch_agent": HIGH_CAPABILITY_MODEL_ID,
    "alert_agent": LOW_COST_MODEL_ID,
    "safety_qa_agent": LOW_COST_MODEL_ID,
    "knowledge_agent": LOW_COST_MODEL_ID,
    "coordinator_orchestrator": HIGH_CAPABILITY_MODEL_ID,
}

assert set(MODEL_FOR_ROLE) == AGENT_ROLES, (
    "MODEL_FOR_ROLE must cover exactly the seven declared agent roles"
)


# ---------------------------------------------------------------------------
# Incident_Graph execution limits (Req 4.4). The three limits the graph
# enforces per run: overall run timeout, per-node timeout, and max node
# executions. Each has a spec-mandated default and an inclusive configurable
# range; a value outside its range is clamped-with-error (rejected) rather
# than silently accepted, so a misconfiguration fails loudly at build time
# instead of producing an out-of-spec run. Read from env so a deployer can
# tune them without a code change (Req 4.4 "configurable"), defaulting to the
# spec defaults when the env var is unset or unparseable.
#
# These live here (the single config module) rather than inside
# `agents/incident_graph.py` so `build_incident_graph()` wires them from one
# source, mirroring how every agent reads its model id from this module.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphLimit:
    """One configurable Incident_Graph execution limit (Req 4.4).

    Attributes:
        name: Human-readable name for error messages.
        default: The spec-mandated default applied when no override is given.
        minimum: Inclusive lower bound of the configurable range.
        maximum: Inclusive upper bound of the configurable range.
    """

    name: str
    default: int
    minimum: int
    maximum: int

    def resolve(self, override: int | None = None) -> int:
        """Return the effective value, validating it against the range.

        Args:
            override: An explicit value to use instead of `self.default`.
                `None` selects the default (always in range by construction).

        Returns:
            The resolved integer value.

        Raises:
            ValueError: If `override` falls outside ``[minimum, maximum]``.
        """
        value = self.default if override is None else override
        if not (self.minimum <= value <= self.maximum):
            raise ValueError(
                f"{self.name} must be in range "
                f"[{self.minimum}, {self.maximum}] seconds/executions; got {value}"
            )
        return value


# Req 4.4 exact defaults and ranges (requirements.md §4.4).
EXECUTION_TIMEOUT_LIMIT: Final[GraphLimit] = GraphLimit(
    name="Incident_Graph overall run execution timeout",
    default=600,
    minimum=60,
    maximum=1800,
)
NODE_TIMEOUT_LIMIT: Final[GraphLimit] = GraphLimit(
    name="Incident_Graph per-node execution timeout",
    default=120,
    minimum=10,
    maximum=600,
)
MAX_NODE_EXECUTIONS_LIMIT: Final[GraphLimit] = GraphLimit(
    name="Incident_Graph maximum node executions per run",
    default=25,
    minimum=6,
    maximum=100,
)


def _env_int(var_name: str) -> int | None:
    """Read an integer env override, or `None` if unset/unparseable.

    An unparseable value is treated as "unset" (returns `None`, selecting the
    default) rather than raising here, so a stray non-numeric env value falls
    back to the safe spec default; the range check in `GraphLimit.resolve`
    still rejects an in-numeric-but-out-of-range override.
    """
    raw = os.environ.get(var_name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_graph_execution_limits() -> tuple[int, int, int]:
    """Resolve the three Incident_Graph execution limits (Req 4.4).

    Reads optional env overrides (`THUNAI_GRAPH_EXECUTION_TIMEOUT_S`,
    `THUNAI_GRAPH_NODE_TIMEOUT_S`, `THUNAI_GRAPH_MAX_NODE_EXECUTIONS`),
    each validated against its configurable range; an unset/blank/unparseable
    override selects the spec default.

    Returns:
        A ``(execution_timeout_s, node_timeout_s, max_node_executions)``
        tuple, each value guaranteed within its Req 4.4 range.

    Raises:
        ValueError: If any provided override is numeric but out of range.
    """
    return (
        EXECUTION_TIMEOUT_LIMIT.resolve(_env_int("THUNAI_GRAPH_EXECUTION_TIMEOUT_S")),
        NODE_TIMEOUT_LIMIT.resolve(_env_int("THUNAI_GRAPH_NODE_TIMEOUT_S")),
        MAX_NODE_EXECUTIONS_LIMIT.resolve(_env_int("THUNAI_GRAPH_MAX_NODE_EXECUTIONS")),
    )


def get_model_id_for_role(role: str) -> str:
    """Return the Bedrock model id a given agent role should use.

    Args:
        role: One of the keys in `MODEL_FOR_ROLE` (e.g. `"monitor_agent"`).

    Returns:
        The resolved model id string for that role.

    Raises:
        ValueError: If `role` is not a member of `MODEL_FOR_ROLE`, naming the
            unknown role and the full set of valid roles.
    """
    try:
        return MODEL_FOR_ROLE[role]
    except KeyError as exc:
        raise ValueError(
            f"Unknown agent role {role!r}; must be one of {sorted(MODEL_FOR_ROLE)}"
        ) from exc


def validate_config(
    model_for_role: dict[str, str] | None = None,
    *,
    low_cost_model_id: str | None = None,
    high_capability_model_id: str | None = None,
) -> None:
    """Validate model-routing configuration at cold start (Req 19.9, 20.6).

    Mirrors `policy.escalation_policy.validate_policy()` in spirit (design.md
    §3.4/Design Question 1): collects *every* problem found rather than
    raising on the first one, then raises a single exception naming all of
    them, so a deployer sees the complete list of what to fix in one pass.

    Checks performed:
        1. Both `THUNAI_LOW_COST_MODEL_ID` and `THUNAI_HIGH_CAPABILITY_MODEL_ID`
           (or the values passed via the keyword-only parameters below) are
           set to non-empty strings.
        2. `MODEL_FOR_ROLE` (or the mapping passed via `model_for_role`)
           covers every one of the seven declared agent roles in
           `AGENT_ROLES`, with no missing and no extra entries.

    Args:
        model_for_role: The role -> model-id mapping to validate. Defaults
            to the module-level `MODEL_FOR_ROLE` built from the current
            environment; a caller (e.g. a test) may pass an alternate
            mapping to validate without mutating process environment state.
        low_cost_model_id: The low-cost model id to validate. Defaults to
            the module-level `LOW_COST_MODEL_ID` (i.e. the current value of
            `THUNAI_LOW_COST_MODEL_ID`).
        high_capability_model_id: The high-capability model id to validate.
            Defaults to the module-level `HIGH_CAPABILITY_MODEL_ID` (i.e. the
            current value of `THUNAI_HIGH_CAPABILITY_MODEL_ID`).

    Raises:
        ValueError: Naming every problem found, one per line, if any check
            fails. Raises nothing (returns `None`) when every check passes.
    """
    if model_for_role is None:
        model_for_role = MODEL_FOR_ROLE
    if low_cost_model_id is None:
        low_cost_model_id = LOW_COST_MODEL_ID
    if high_capability_model_id is None:
        high_capability_model_id = HIGH_CAPABILITY_MODEL_ID

    problems: list[str] = []

    if not low_cost_model_id:
        problems.append("THUNAI_LOW_COST_MODEL_ID is not set (or is empty)")
    if not high_capability_model_id:
        problems.append("THUNAI_HIGH_CAPABILITY_MODEL_ID is not set (or is empty)")

    mapped_roles = set(model_for_role)
    missing_roles = AGENT_ROLES - mapped_roles
    extra_roles = mapped_roles - AGENT_ROLES
    if missing_roles:
        problems.append(f"MODEL_FOR_ROLE is missing role(s): {sorted(missing_roles)}")
    if extra_roles:
        problems.append(
            f"MODEL_FOR_ROLE has unrecognised role(s): {sorted(extra_roles)}"
        )

    for role, model_id in model_for_role.items():
        if role in AGENT_ROLES and not model_id:
            problems.append(f"MODEL_FOR_ROLE[{role!r}] is not set (or is empty)")

    if problems:
        raise ValueError(
            "agents.config.validate_config found "
            f"{len(problems)} problem(s): " + "; ".join(problems)
        )
