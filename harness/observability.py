"""Observability_Layer: per-run tracing, metrics recording, and budget notification (Req 20).

design.md's **Cost Model** and **§3.11** (``observability_stack.py``,
"Transaction Search enablement, budget alarm") assign this module the runtime
side of Requirement 20 — the *code* that emits a trace per run, records the
run's token/latency/cost figures where ``Coordinator_Console`` can read them,
and delivers the in-run budget notification. The *infrastructure* side (the
account-level ``AWS Budgets`` alarm of Req 20.4, and Transaction Search
enablement) is ``observability_stack.py`` (task 21.x); this module never
provisions anything, it only emits/records/notifies at run time.

This is a **harness/surface-layer** module (design.md "Repository layout"
places redaction, hooks, and audit under ``harness/``; observability sits
alongside them because it is another cross-cutting reliability concern applied
around every run, not agent logic). It composes three already-built pieces
rather than re-implementing them:

- ``harness.redaction.redact_fields`` — every span attribute and every recorded
  metric field is passed through it before emission (Req 20.2: "SHALL record no
  resident free-text content and no credential value in any span attribute").
- ``memory.state_store.update_run_record`` — the queryable run-index item the
  console's "20 most recent runs with cost" list (Req 1.7/12.8) reads through
  ``surface/queries.py``; writing the metrics there is what "exposes the
  recorded values to Coordinator_Console" (Req 20.3).
- ``memory.state_store.idempotent_write`` + ``integrations.notification_provider``
  — the budget notification path, keyed so at most one notification is
  delivered per threshold per billing period (Req 20.5).

Requirement 20.9 is the load-bearing invariant of this whole module: **if trace
emission or metric recording fails, the run continues to its terminal status
unchanged.** Every public function here is therefore total — it catches its own
exceptions, records the emission failure (to the logger and, best-effort, to the
run record's ``observability_error`` field), and returns rather than raising.
Observability must never be the reason a hazard run fails.

Strands ``result.metrics`` (Req 20.3 token/latency source): a Strands
``AgentResult``/``NodeResult`` carries an ``EventLoopMetrics`` under
``.metrics`` (or ``.result.metrics`` for a ``NodeResult`` wrapping an
``AgentResult``). That object exposes ``accumulated_usage`` — a mapping with
``inputTokens``/``outputTokens``/``totalTokens`` — and ``accumulated_metrics``
with ``latencyMs``. This module reads those fields defensively (via
``getattr``/``dict`` access with fallbacks) so a change in the exact metrics
shape degrades to a zeroed/omitted figure rather than crashing the run — again
Req 20.9.

OTel/ADOT wiring: the deployed runtime is launched under the ADOT distro's
``opentelemetry-instrument`` wrapper (an ``observability_stack.py`` / runtime
launch concern, not a code import), and Strands' own instrumentation
(``strands.telemetry``) emits spans for agent/model/tool activity when a tracer
provider is present. This module adds the **one run-level span per run** that
Req 20.1 requires as the parent, plus explicit child spans per node/tool/model
derived from the finished ``RunResult`` so the trace is complete even when a
node's own auto-instrumentation is absent. Acquiring the tracer is best-effort:
if the OpenTelemetry API is not importable (e.g. a minimal test env), the
tracer degrades to a no-op and metric recording still proceeds.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Final, Iterator

from harness.redaction import redact_fields

logger = logging.getLogger("thunai.observability")

__all__ = [
    "RunMetrics",
    "extract_run_metrics",
    "record_run_metrics",
    "emit_run_trace",
    "observe_run",
    "maybe_notify_budget",
    "DEFAULT_CURRENCY",
]

# ---------------------------------------------------------------------------
# Configuration (per-token pricing + budget threshold), all env-driven so a
# deployer tunes them without a code change, mirroring agents/config.py.
# ---------------------------------------------------------------------------

DEFAULT_CURRENCY: Final[str] = "USD"

#: Env var holding the ISO-4217 currency unit costs are expressed in (Req 20.3).
_CURRENCY_ENV: Final[str] = "THUNAI_COST_CURRENCY"

#: Per-1000-token input/output price for each of the two configured model tiers
#: (Req 20.3 "the configured per-token price rate of each invoked model
#: identifier"). Prices are per 1K tokens in the configured currency's *minor*
#: pricing scale (i.e. plain currency units, e.g. USD), read from env so the
#: real Bedrock Nova price list is supplied by the deployer rather than baked in.
_PRICE_ENV: Final[dict[str, tuple[str, str]]] = {
    # model-tier key -> (input-price env var, output-price env var)
    "low_cost": ("THUNAI_LOW_COST_INPUT_PRICE_PER_1K", "THUNAI_LOW_COST_OUTPUT_PRICE_PER_1K"),
    "high_capability": (
        "THUNAI_HIGH_CAPABILITY_INPUT_PRICE_PER_1K",
        "THUNAI_HIGH_CAPABILITY_OUTPUT_PRICE_PER_1K",
    ),
}

#: The per-billing-period spend threshold that triggers the in-run budget
#: notification (Req 20.5). Expressed in *minor* currency units (e.g. cents) to
#: match harness/hooks.py's SpendCapHook convention. design.md's Cost Model
#: names $20 as the account-level backstop; this in-run notification threshold
#: defaults to the same figure so the two agree unless a deployer overrides it.
_BUDGET_THRESHOLD_ENV: Final[str] = "THUNAI_BUDGET_THRESHOLD_MINOR_UNITS"
_DEFAULT_BUDGET_THRESHOLD_MINOR_UNITS: Final[int] = 2000  # $20.00

#: Where the budget notification is delivered (Req 20.5 "the configured
#: address"). Reuses the coordinator contact vars the escalation path uses.
_BUDGET_EMAIL_ENV: Final[str] = "THUNAI_COORDINATOR_EMAIL"
_BUDGET_PHONE_ENV: Final[str] = "THUNAI_COORDINATOR_PHONE"


def _env_float(name: str) -> float:
    raw = os.environ.get(name)
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        logger.warning("Observability price env %s is not a number: %r", name, raw)
        return 0.0


def _tier_for_model_id(model_id: str | None) -> str | None:
    """Map an invoked model id back to its configured tier (low/high).

    Read lazily from ``agents.config`` so this module has no import cycle with
    it and so a missing config does not stop metric recording — an unknown id
    simply yields ``None`` (priced at 0, still counted in tokens/latency).
    """
    if not model_id:
        return None
    try:
        from agents import config as agent_config

        if model_id == agent_config.LOW_COST_MODEL_ID:
            return "low_cost"
        if model_id == agent_config.HIGH_CAPABILITY_MODEL_ID:
            return "high_capability"
    except Exception as exc:  # noqa: BLE001 - config read must not break recording
        logger.debug("Model-tier lookup failed for %r: %r", model_id, exc)
    return None


# ---------------------------------------------------------------------------
# The recorded per-run metrics (Req 20.3).
# ---------------------------------------------------------------------------


@dataclass
class RunMetrics:
    """The token/latency/cost figures recorded for one run (Req 20.3).

    Attributes:
        run_id: The run these figures belong to.
        input_tokens: Summed input token count across every model invocation.
        output_tokens: Summed output token count across every model invocation.
        total_tokens: ``input_tokens + output_tokens``.
        latency_ms: Wall-clock latency of the run in milliseconds.
        model_ids: Every distinct model identifier invoked during the run.
        estimated_cost: Estimated cost in :attr:`currency`, derived from the
            per-model token counts and the configured per-token price of each
            invoked model identifier.
        currency: The ISO-4217 currency unit :attr:`estimated_cost` is in.
    """

    run_id: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    model_ids: list[str] = field(default_factory=list)
    estimated_cost: float = 0.0
    currency: str = DEFAULT_CURRENCY

    def as_record_fields(self) -> dict[str, Any]:
        """The run-index fields this maps to (Req 20.3, read by the console).

        The estimated cost is also stored in *minor* currency units
        (``estimated_cost_minor_units``) so the console and the SpendCapHook
        speak the same integer scale ``memory/state_store.py`` documents.
        """
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "model_ids": list(self.model_ids),
            "estimated_cost": round(self.estimated_cost, 6),
            "estimated_cost_minor_units": int(round(self.estimated_cost * 100)),
            "currency": self.currency,
            "metrics_recorded_at": datetime.now(timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Metric extraction from a finished run (reads Strands result.metrics).
# ---------------------------------------------------------------------------


def _usage_from_metrics(metrics: Any) -> tuple[int, int, int]:
    """Return ``(input, output, total)`` tokens from a Strands metrics object.

    Reads ``metrics.accumulated_usage`` — a mapping with
    ``inputTokens``/``outputTokens``/``totalTokens`` on a Strands
    ``EventLoopMetrics``. Defensive: any missing/oddly-shaped field degrades to
    0 rather than raising, so a metrics-shape change cannot break the run
    (Req 20.9).
    """
    usage = getattr(metrics, "accumulated_usage", None)
    if usage is None and isinstance(metrics, dict):
        usage = metrics.get("accumulated_usage")
    if usage is None:
        return (0, 0, 0)

    def _get(key: str) -> int:
        try:
            value = usage[key] if isinstance(usage, dict) else getattr(usage, key)
        except (KeyError, AttributeError, TypeError):
            return 0
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    input_tokens = _get("inputTokens")
    output_tokens = _get("outputTokens")
    total = _get("totalTokens") or (input_tokens + output_tokens)
    return (input_tokens, output_tokens, total)


def _latency_from_metrics(metrics: Any) -> int:
    """Return ``latencyMs`` from a Strands metrics object, else 0."""
    accumulated = getattr(metrics, "accumulated_metrics", None)
    if accumulated is None and isinstance(metrics, dict):
        accumulated = metrics.get("accumulated_metrics")
    if accumulated is None:
        return 0
    try:
        value = (
            accumulated["latencyMs"]
            if isinstance(accumulated, dict)
            else getattr(accumulated, "latencyMs")
        )
        return int(value)
    except (KeyError, AttributeError, TypeError, ValueError):
        return 0


def _iter_node_metrics(run_result: Any) -> Iterator[tuple[str | None, Any]]:
    """Yield ``(model_id, metrics_obj)`` for every model invocation in a run.

    Walks the ``RunResult.node_executions`` mapping (each ``NodeExecution``
    holds the node's Strands ``NodeResult`` under ``.result``), reaching the
    ``EventLoopMetrics`` at ``node_result.result.metrics`` (a ``NodeResult``
    wrapping an ``AgentResult``) or ``node_result.metrics`` directly.
    Deterministic ``Rule_Engine`` nodes make zero model calls and carry no
    usable metrics — they simply yield nothing.
    """
    node_execs = getattr(run_result, "node_executions", None) or {}
    for node_exec in node_execs.values():
        node_result = getattr(node_exec, "result", None)
        if node_result is None:
            continue
        # NodeResult -> AgentResult -> metrics, or NodeResult.metrics directly.
        agent_result = getattr(node_result, "result", None)
        metrics = getattr(agent_result, "metrics", None) or getattr(node_result, "metrics", None)
        if metrics is None:
            continue
        model_id = (
            getattr(agent_result, "model_id", None)
            or getattr(node_result, "model_id", None)
        )
        yield (model_id, metrics)


def _cost_for(model_id: str | None, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost for one model invocation from its tier's per-1K price."""
    tier = _tier_for_model_id(model_id)
    if tier is None:
        return 0.0
    input_env, output_env = _PRICE_ENV[tier]
    input_price = _env_float(input_env)
    output_price = _env_float(output_env)
    return (input_tokens / 1000.0) * input_price + (output_tokens / 1000.0) * output_price


def extract_run_metrics(
    run_result: Any,
    *,
    run_id: str,
    latency_ms: int | None = None,
) -> RunMetrics:
    """Build a :class:`RunMetrics` from a finished run (Req 20.3).

    Sums the token counts and per-model estimated cost across every model
    invocation the run made (read from Strands ``result.metrics``), collects
    the distinct invoked model identifiers, and records the run's wall-clock
    latency.

    Args:
        run_result: The finished ``agents.incident_graph.RunResult`` (or any
            object exposing a ``node_executions`` mapping of the same shape).
            A run with no model calls (e.g. a pure ``Rule_Engine`` sweep)
            yields zeroed token/cost figures — which is correct.
        run_id: The run identifier.
        latency_ms: The run's wall-clock latency, measured by the caller (the
            entrypoint times the whole invocation). When omitted, the summed
            per-node latency from ``result.metrics`` is used as a fallback.

    Returns:
        A populated :class:`RunMetrics`. Never raises: an unexpected metrics
        shape degrades to zeros (Req 20.9).
    """
    metrics = RunMetrics(run_id=run_id, currency=os.environ.get(_CURRENCY_ENV, DEFAULT_CURRENCY))
    model_ids: list[str] = []
    node_latency_total = 0
    try:
        for model_id, metrics_obj in _iter_node_metrics(run_result):
            in_tok, out_tok, _total = _usage_from_metrics(metrics_obj)
            metrics.input_tokens += in_tok
            metrics.output_tokens += out_tok
            node_latency_total += _latency_from_metrics(metrics_obj)
            metrics.estimated_cost += _cost_for(model_id, in_tok, out_tok)
            if model_id and model_id not in model_ids:
                model_ids.append(model_id)
    except Exception as exc:  # noqa: BLE001 - Req 20.9: degrade, never raise
        logger.warning("Run-metric extraction degraded for run_id=%s: %r", run_id, exc)

    metrics.total_tokens = metrics.input_tokens + metrics.output_tokens
    metrics.model_ids = model_ids
    metrics.latency_ms = int(latency_ms) if latency_ms is not None else node_latency_total
    return metrics


# ---------------------------------------------------------------------------
# Recording (exposes metrics to Coordinator_Console, Req 20.3).
# ---------------------------------------------------------------------------


def record_run_metrics(
    metrics: RunMetrics,
    *,
    update_run_record: Callable[..., Any] | None = None,
) -> bool:
    """Persist a run's metrics to its run-index item (Req 20.3).

    Writing to the run record is what "exposes the recorded values to
    Coordinator_Console within 5 seconds" (Req 20.3) — the console's recent-runs
    list (``surface/queries.py`` over ``state_store.query_recent_runs``) reads
    the same item, so the figures are visible on the next list read, far inside
    the 5s budget.

    Args:
        metrics: The :class:`RunMetrics` to persist.
        update_run_record: Injectable writer (test seam); defaults to
            ``memory.state_store.update_run_record``.

    Returns:
        ``True`` if the write succeeded, ``False`` if it failed. **Never
        raises** — a recording failure is logged and swallowed so the run
        continues unchanged (Req 20.9).
    """
    writer = update_run_record
    if writer is None:
        from memory import state_store

        writer = state_store.update_run_record
    try:
        writer(metrics.run_id, **redact_fields(metrics.as_record_fields()))
        return True
    except Exception as exc:  # noqa: BLE001 - Req 20.9
        logger.error("Run-metric recording failed for run_id=%s: %r", metrics.run_id, exc)
        _record_emission_failure(metrics.run_id, f"metric recording failed: {exc}")
        return False


def _record_emission_failure(run_id: str, detail: str) -> None:
    """Record an observability emission failure without ever raising (Req 20.9).

    Req 20.9 requires the *Observability_Layer* to record the emission failure.
    Best-effort: attach it to the run-index item so it is visible next to the
    run's other fields; if even that write fails, the logger line above is the
    durable record and we give up silently rather than looping.
    """
    try:
        from memory import state_store

        state_store.update_run_record(
            run_id,
            observability_error=detail[:500],
            observability_error_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001 - the logger line is the durable record
        logger.debug("Could not persist observability_error for run_id=%s: %r", run_id, exc)


# ---------------------------------------------------------------------------
# Tracing (one trace per run, spans per node/tool/model, redacted attrs; Req 20.1/20.2).
# ---------------------------------------------------------------------------


def _get_tracer() -> Any | None:
    """Return an OpenTelemetry tracer, or ``None`` if OTel is unavailable.

    Best-effort acquisition: in the deployed runtime the ADOT
    ``opentelemetry-instrument`` wrapper (an ``observability_stack.py`` / launch
    concern) has already configured a tracer provider, and Strands'
    ``strands.telemetry`` instrumentation is active; here we just fetch the
    global tracer. In a minimal environment where the OTel API is not
    importable, this returns ``None`` and every tracing call degrades to a
    no-op — the run and its metric recording are unaffected (Req 20.9).
    """
    try:
        from opentelemetry import trace as _otel_trace

        return _otel_trace.get_tracer("thunai.observability")
    except Exception as exc:  # noqa: BLE001 - OTel absence is not a run failure
        logger.debug("OpenTelemetry tracer unavailable; tracing is a no-op: %r", exc)
        return None


def _safe_set_attributes(span: Any, attributes: dict[str, Any]) -> None:
    """Set span attributes, redacting them first (Req 20.2) and never raising."""
    try:
        for key, value in redact_fields(attributes).items():
            if value is None:
                continue
            # OTel accepts str/bool/int/float (and homogeneous sequences); stringify
            # anything else so no attribute set can fail on an unsupported type.
            if isinstance(value, (str, bool, int, float)):
                span.set_attribute(key, value)
            elif isinstance(value, (list, tuple)) and all(
                isinstance(v, str) for v in value
            ):
                span.set_attribute(key, list(value))
            else:
                span.set_attribute(key, str(value))
    except Exception as exc:  # noqa: BLE001 - Req 20.9
        logger.debug("Setting span attributes degraded: %r", exc)


def emit_run_trace(
    run_result: Any,
    *,
    run_id: str,
    session_id: str | None,
    incident_id: str | None,
    metrics: RunMetrics | None = None,
) -> bool:
    """Emit exactly one trace for a finished run (Req 20.1, 20.2).

    The trace is a single run-level span carrying the run/session/incident
    identifiers (Req 20.2) with one child span per executed node, and — derived
    from the node's Strands metrics — one child span per model invocation. Tool
    spans emitted by Strands' own instrumentation attach to the same trace when
    a live tracer provider is present; this function guarantees the run/node/
    model spans exist even when auto-instrumentation is not.

    Every attribute is passed through ``redact_fields`` before it is set, so no
    resident free-text and no credential value ever reaches a span attribute
    (Req 20.2). No node input/output text is put on any span at all — only ids,
    statuses, counts, and durations.

    Returns:
        ``True`` if a trace was emitted, ``False`` if tracing was unavailable or
        emission failed. **Never raises** (Req 20.9).
    """
    tracer = _get_tracer()
    if tracer is None:
        return False
    try:
        with tracer.start_as_current_span("thunai.run") as run_span:
            _safe_set_attributes(
                run_span,
                {
                    "thunai.run_id": run_id,
                    "thunai.session_id": session_id,
                    "thunai.incident_id": incident_id or "no-incident",
                    "thunai.outcome": getattr(run_result, "outcome", None),
                    "thunai.input_tokens": metrics.input_tokens if metrics else None,
                    "thunai.output_tokens": metrics.output_tokens if metrics else None,
                    "thunai.total_tokens": metrics.total_tokens if metrics else None,
                    "thunai.latency_ms": metrics.latency_ms if metrics else None,
                    "thunai.estimated_cost": metrics.estimated_cost if metrics else None,
                    "thunai.currency": metrics.currency if metrics else None,
                    "thunai.model_ids": metrics.model_ids if metrics else None,
                },
            )
            _emit_node_spans(tracer, run_result, run_id=run_id, incident_id=incident_id)
        return True
    except Exception as exc:  # noqa: BLE001 - Req 20.9
        logger.error("Run-trace emission failed for run_id=%s: %r", run_id, exc)
        _record_emission_failure(run_id, f"trace emission failed: {exc}")
        return False


def _emit_node_spans(
    tracer: Any, run_result: Any, *, run_id: str, incident_id: str | None
) -> None:
    """Emit one span per executed node and one per model invocation within it."""
    node_execs = getattr(run_result, "node_executions", None) or {}
    execution_order = getattr(run_result, "execution_order", None) or list(node_execs.keys())
    node_status = getattr(run_result, "node_status", None) or {}
    for node_id in execution_order:
        node_exec = node_execs.get(node_id)
        with tracer.start_as_current_span(f"thunai.node.{node_id}") as node_span:
            _safe_set_attributes(
                node_span,
                {
                    "thunai.run_id": run_id,
                    "thunai.node_id": node_id,
                    "thunai.node_status": node_status.get(node_id)
                    or getattr(node_exec, "status", None),
                    "thunai.incident_id": incident_id or "no-incident",
                },
            )
            node_result = getattr(node_exec, "result", None) if node_exec else None
            agent_result = getattr(node_result, "result", None) if node_result else None
            metrics_obj = (
                getattr(agent_result, "metrics", None)
                or getattr(node_result, "metrics", None)
            )
            if metrics_obj is None:
                continue
            model_id = (
                getattr(agent_result, "model_id", None)
                or getattr(node_result, "model_id", None)
            )
            in_tok, out_tok, total = _usage_from_metrics(metrics_obj)
            with tracer.start_as_current_span(f"thunai.model.{node_id}") as model_span:
                _safe_set_attributes(
                    model_span,
                    {
                        "thunai.run_id": run_id,
                        "thunai.node_id": node_id,
                        "thunai.model_id": model_id,
                        "thunai.input_tokens": in_tok,
                        "thunai.output_tokens": out_tok,
                        "thunai.total_tokens": total,
                        "thunai.latency_ms": _latency_from_metrics(metrics_obj),
                    },
                )


# ---------------------------------------------------------------------------
# Budget notification (Req 20.5): at most one per threshold per billing period.
# ---------------------------------------------------------------------------


def _billing_period(now: datetime | None = None) -> str:
    """The current billing period key (``YYYY-MM``), for the once-per-period cap."""
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%Y-%m")


def _accrued_spend_minor_units(period: str) -> int:
    """Sum recorded per-run estimated cost (minor units) over the billing period.

    Reads the recent run records and sums ``estimated_cost_minor_units`` for
    runs whose ``metrics_recorded_at`` falls in ``period``. Best-effort: any
    read failure yields 0 (no notification fired on a read we could not do),
    logged, never raised (Req 20.9).
    """
    try:
        from memory import state_store

        total = 0
        for record in state_store.query_recent_runs(limit=100):
            recorded_at = str(record.get("metrics_recorded_at") or record.get("started_at") or "")
            if recorded_at[:7] != period:
                continue
            try:
                total += int(record.get("estimated_cost_minor_units") or 0)
            except (TypeError, ValueError):
                continue
        return total
    except Exception as exc:  # noqa: BLE001 - Req 20.9
        logger.warning("Accrued-spend read failed for period %s: %r", period, exc)
        return 0


def maybe_notify_budget(
    *,
    run_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deliver the budget notification if accrued spend reached the threshold (Req 20.5).

    Sums the accrued estimated spend for the current billing period; if it has
    reached the configured threshold, delivers one notification to the
    configured coordinator address(es). The delivery is wrapped in
    ``state_store.idempotent_write`` keyed on
    ``budget_notification:{period}:{threshold}`` so **at most one** notification
    is delivered per threshold per billing period (Req 20.5) even across
    processes and retries.

    Returns:
        A dict describing what happened (``notified`` / ``reason`` /
        ``accrued_minor_units`` / ``threshold_minor_units``). **Never raises**
        (Req 20.9).
    """
    try:
        threshold = _budget_threshold_minor_units()
        period = _billing_period(now)
        accrued = _accrued_spend_minor_units(period)
        if accrued < threshold:
            return {
                "notified": False,
                "reason": "below_threshold",
                "accrued_minor_units": accrued,
                "threshold_minor_units": threshold,
                "period": period,
            }

        from memory import state_store

        key = f"budget_notification:{period}:{threshold}"
        outcome = state_store.idempotent_write(
            key,
            lambda: _deliver_budget_notification(
                period=period,
                accrued_minor_units=accrued,
                threshold_minor_units=threshold,
                run_id=run_id,
            ),
        )
        return dict(outcome)
    except Exception as exc:  # noqa: BLE001 - Req 20.9: a budget-check failure never breaks the run
        logger.error("Budget-notification check failed for run_id=%s: %r", run_id, exc)
        _record_emission_failure(run_id, f"budget notification check failed: {exc}")
        return {"notified": False, "reason": f"error: {exc}"}


def _budget_threshold_minor_units() -> int:
    raw = os.environ.get(_BUDGET_THRESHOLD_ENV)
    if not raw:
        return _DEFAULT_BUDGET_THRESHOLD_MINOR_UNITS
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "%s is not an integer: %r; using default", _BUDGET_THRESHOLD_ENV, raw
        )
        return _DEFAULT_BUDGET_THRESHOLD_MINOR_UNITS


def _deliver_budget_notification(
    *,
    period: str,
    accrued_minor_units: int,
    threshold_minor_units: int,
    run_id: str,
) -> dict[str, Any]:
    """Send the budget notification to the configured address(es) (Req 20.5).

    Called exactly once per threshold per billing period (the surrounding
    ``idempotent_write`` guarantees that). Delivers to whichever of the
    configured email/phone addresses is set; a missing address is skipped
    rather than an error.
    """
    currency = os.environ.get(_CURRENCY_ENV, DEFAULT_CURRENCY)
    amount = accrued_minor_units / 100.0
    threshold_amount = threshold_minor_units / 100.0
    subject = f"ThunAI budget threshold reached for {period}"
    body = (
        f"ThunAI estimated spend for billing period {period} has reached "
        f"{amount:.2f} {currency}, at or above the configured threshold of "
        f"{threshold_amount:.2f} {currency}. This is the single notification for "
        f"this threshold this billing period."
    )
    idem_base = f"budget:{period}:{threshold_minor_units}"

    email = os.environ.get(_BUDGET_EMAIL_ENV, "").strip()
    phone = os.environ.get(_BUDGET_PHONE_ENV, "").strip()
    channels: list[str] = []
    try:
        from integrations.notification_provider import notification_provider

        provider = notification_provider()
        if email:
            provider.send_email(email, subject, body, f"{idem_base}:email")
            channels.append("email")
        if phone:
            provider.send_sms(phone, body, f"{idem_base}:sms")
            channels.append("sms")
    except Exception as exc:  # noqa: BLE001 - Req 20.9: delivery failure never breaks the run
        logger.error("Budget notification delivery failed for %s: %r", period, exc)
        return {
            "notified": False,
            "reason": f"delivery_error: {exc}",
            "accrued_minor_units": accrued_minor_units,
            "threshold_minor_units": threshold_minor_units,
            "period": period,
        }

    return {
        "notified": bool(channels),
        "reason": "threshold_reached" if channels else "no_configured_address",
        "channels": channels,
        "accrued_minor_units": accrued_minor_units,
        "threshold_minor_units": threshold_minor_units,
        "period": period,
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------
# The one call site the entrypoint uses (wraps everything, total by design).
# ---------------------------------------------------------------------------


@contextmanager
def observe_run(
    *,
    run_id: str,
    session_id: str | None,
    incident_id: str | None,
) -> Iterator["_RunObservation"]:
    """Wrap one run: record metrics + emit a trace + check the budget on exit.

    Usage in ``surface/entrypoint.py``::

        with observe_run(run_id=run_id, session_id=session_id, incident_id=incident_id) as obs:
            result = run_the_graph(...)
            obs.set_result(result, latency_ms=elapsed_ms, incident_id=result_incident_id)

    On exit the context manager extracts the run's metrics from the recorded
    ``RunResult``, persists them (Req 20.3), emits one trace (Req 20.1/20.2),
    and checks the per-period budget (Req 20.5). It is **total**: any failure in
    any of those steps is caught and recorded, and the ``with`` block returns
    normally so the run's own outcome is unchanged (Req 20.9). If no result was
    set (the run raised before completing), the wrap-up quietly does nothing —
    the entrypoint's own failure path already recorded the failure.
    """
    observation = _RunObservation(
        run_id=run_id, session_id=session_id, incident_id=incident_id
    )
    try:
        yield observation
    finally:
        observation.finish()


@dataclass
class _RunObservation:
    """Mutable holder threaded through :func:`observe_run` (internal)."""

    run_id: str
    session_id: str | None
    incident_id: str | None
    _run_result: Any = None
    _latency_ms: int | None = None
    _done: bool = False

    def set_result(
        self,
        run_result: Any,
        *,
        latency_ms: int | None = None,
        incident_id: str | None = None,
    ) -> None:
        """Record the finished run so :meth:`finish` can extract its metrics."""
        self._run_result = run_result
        self._latency_ms = latency_ms
        if incident_id is not None:
            self.incident_id = incident_id

    def finish(self) -> RunMetrics | None:
        """Extract + record metrics, emit the trace, check the budget (Req 20).

        Idempotent and total: safe to call once (the context manager does), a
        no-op if no result was set, and it never propagates an exception
        (Req 20.9).
        """
        if self._done or self._run_result is None:
            return None
        self._done = True
        try:
            metrics = extract_run_metrics(
                self._run_result, run_id=self.run_id, latency_ms=self._latency_ms
            )
            record_run_metrics(metrics)
            emit_run_trace(
                self._run_result,
                run_id=self.run_id,
                session_id=self.session_id,
                incident_id=self.incident_id,
                metrics=metrics,
            )
            maybe_notify_budget(run_id=self.run_id)
            return metrics
        except Exception as exc:  # noqa: BLE001 - Req 20.9: the whole wrap-up is best-effort
            logger.error("observe_run wrap-up degraded for run_id=%s: %r", self.run_id, exc)
            _record_emission_failure(self.run_id, f"observe_run wrap-up failed: {exc}")
            return None
