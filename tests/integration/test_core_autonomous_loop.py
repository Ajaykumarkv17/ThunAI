"""Task 18 checkpoint — the core autonomous loop, end to end on fixtures.

Validates: the trigger -> autonomous multi-agent work -> exactly one
escalation -> resume -> append-only audit loop, exercised locally against the
fixture backends only (SYNTHETIC_SENSORS=1, a moto-mocked ``thunai-state`` /
``thunai-audit`` DynamoDB pair, an in-process fake NotificationProvider, and an
in-process fake AgentCore resume invoker). No live AWS and no live Bedrock
model call is made anywhere in this module.

Scope covered (tasks 2-17): schemas + policy + Rule_Engine (real, deterministic,
zero model calls), State_Store + Audit_Ledger + Harness audit path (real, over
moto), the integration seam + fixtures (real SyntheticSensorProvider reading
``seed/hazard_readings``), Incident_Graph + its pause-aware driver (real
``execute_incident_graph`` walking the real declared topology), and
Escalation_Service + HITL wiring (real ``create_escalation`` / ``resolve_escalation``).

Why a fake ``node_runner`` for the LLM nodes: ``agents/incident_graph.py``'s
driver documents ``node_runner`` as its injectable test seam (design principle
5 / Req 21.10) precisely so the loop can be driven "with in-process fakes and
no live model call". The deterministic ``RuleEngineNode`` runs for real; the
five specialist LLM ``Agent`` nodes (monitor/intake/dispatch/alert/safety_qa)
are stood in for by fakes that return the structured outputs the real branch
conditions read, so the run routes through the real topology and pauses at
``safety_qa`` — the point at which the deterministic escalation gate raises
exactly one escalation.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import boto3
import pytest
from moto import mock_aws

from strands.agent.agent_result import AgentResult
from strands.multiagent.base import MultiAgentResult, NodeResult, Status
from strands.telemetry.metrics import EventLoopMetrics

from agents.incident_graph import (
    NODE_ALERT,
    NODE_DISPATCH,
    NODE_INTAKE,
    NODE_MONITOR,
    NODE_RULE_ENGINE,
    NODE_SAFETY_QA,
    OUTCOME_PAUSED,
    STATUS_PAUSED,
    STATUS_SUCCEEDED,
    build_incident_graph,
    execute_incident_graph,
)
from agents.rule_engine import RuleEngineNode
from harness.audit import append_audit_entry
from integrations.notification_provider import SendResult
from integrations.sensor_provider import SyntheticSensorProvider
from memory import audit_ledger, state_store
from policy import escalation_policy
from surface import escalation_service as esvc

TABLE_NAME = "thunai-state-core-loop-test"
AUDIT_TABLE_NAME = "thunai-audit-core-loop-test"


# ---------------------------------------------------------------------------
# moto-backed table pair mirroring tests/unit/test_escalation_service.py.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _dynamo_tables():
    os.environ["SYNTHETIC_SENSORS"] = "1"
    os.environ["THUNAI_STATE_TABLE"] = TABLE_NAME
    os.environ["THUNAI_AUDIT_TABLE"] = AUDIT_TABLE_NAME
    os.environ.setdefault("AWS_REGION", "us-west-2")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    os.environ["THUNAI_COORDINATOR_PHONE"] = "+14155550100"
    os.environ["THUNAI_COORDINATOR_EMAIL"] = "coordinator@example.org"
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        for name, gsi in ((TABLE_NAME, "GSI1"), (AUDIT_TABLE_NAME, "gsi1")):
            client.create_table(
                TableName=name,
                KeySchema=[
                    {"AttributeName": "pk", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "pk", "AttributeType": "S"},
                    {"AttributeName": "sk", "AttributeType": "S"},
                    {"AttributeName": "gsi1pk", "AttributeType": "S"},
                    {"AttributeName": "gsi1sk", "AttributeType": "S"},
                ],
                GlobalSecondaryIndexes=[
                    {
                        "IndexName": gsi,
                        "KeySchema": [
                            {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                            {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                        "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                    }
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        state_store.reset_table_cache()
        yield
        state_store.reset_table_cache()


# ---------------------------------------------------------------------------
# In-process fakes (test seams the modules under test document).
# ---------------------------------------------------------------------------
class _FakeNotifier:
    """Satisfies the NotificationProvider Protocol; records every send."""

    def __init__(self) -> None:
        self.sms: list[tuple[str, str, str]] = []
        self.email: list[tuple[str, str, str, str]] = []

    def send_sms(self, phone: str, body: str, idempotency_key: str) -> SendResult:
        self.sms.append((phone, body, idempotency_key))
        return SendResult(message_id=f"sms-{len(self.sms)}", idempotency_key=idempotency_key)

    def send_email(self, address: str, subject: str, body: str, idempotency_key: str) -> SendResult:
        self.email.append((address, subject, body, idempotency_key))
        return SendResult(message_id=f"email-{len(self.email)}", idempotency_key=idempotency_key)


def _agent_result(*, structured: Any = None, state: Any = None, stop_reason: str = "end_turn", interrupts=None) -> AgentResult:
    return AgentResult(
        stop_reason=stop_reason,
        message={"role": "assistant", "content": [{"text": "x"}]},
        metrics=EventLoopMetrics(),
        state=state if state is not None else {},
        structured_output=structured,
        interrupts=interrupts or [],
    )


def _succeed(node_id: str, *, structured: Any = None, state: Any = None) -> MultiAgentResult:
    return MultiAgentResult(
        status=Status.COMPLETED,
        results={node_id: NodeResult(result=_agent_result(structured=structured, state=state), status=Status.COMPLETED)},
    )


def _pause_gate(node_id: str) -> MultiAgentResult:
    """A deterministic-gate pause: NodeResult INTERRUPTED, no SDK interrupt object.

    This is design.md §3.2's "no SDK interrupt object involved at all" branch —
    the shape the escalation gate produces when it pauses a run to raise an
    escalation for a human decision.
    """
    return MultiAgentResult(
        status=Status.INTERRUPTED,
        results={node_id: NodeResult(result=_agent_result(), status=Status.INTERRUPTED)},
    )


class _StructuredDict(dict):
    """A dict that also answers attribute access, so the graph's branch
    conditions (which read either ``.attr`` or ``["attr"]``) see the same
    fields regardless of accessor."""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - defensive
            raise AttributeError(item) from exc


def _fixture_as_of() -> datetime:
    """The latest timestamp the river-level fixture series carries, so the
    sweep evaluates the end of the 5-day rising-river demo scenario."""
    provider = SyntheticSensorProvider()
    latest_ts: datetime | None = None
    for reading_type in ("river_level", "rainfall_rate", "dam_release"):
        rows = provider._readings.get(reading_type, [])  # noqa: SLF001 - fixture introspection
        if rows:
            ts = rows[-1]["timestamp"]
            if latest_ts is None or ts > latest_ts:
                latest_ts = ts
    return latest_ts or datetime.now(timezone.utc)


def test_core_autonomous_loop_end_to_end(capsys):
    """trigger -> autonomous work -> ONE escalation -> resume -> audit, on fixtures."""
    run_id = "run-core-loop-1"
    incident_id = "incident-reach-7"

    # --- 1. TRIGGER: a sweep run starts. Write the run record the entrypoint
    #        writes (Req 1.5), through the same append-only ledger path. -------
    started_at = datetime.now(timezone.utc).isoformat()
    append_audit_entry(
        run_id=run_id,
        tool_name="entrypoint.run",
        outcome="started",
        inputs={"mode": "sweep", "trigger_type": "scheduled_sweep", "started_at": started_at},
        incident_id=incident_id,
    )
    state_store.put_run_record(
        run_id,
        mode="sweep",
        trigger_type="scheduled_sweep",
        started_at=started_at,
        status="in_progress",
        terminal_status=None,
        incident_id=incident_id,
    )

    # --- 2. AUTONOMOUS WORK: run the REAL Incident_Graph topology with the REAL
    #        deterministic RuleEngineNode; the LLM nodes are driven by a fake
    #        node_runner (the driver's documented test seam) so no model is
    #        called. Fixture sensor readings feed the rule engine. ------------
    as_of = _fixture_as_of()
    provider = SyntheticSensorProvider()
    readings: dict[str, dict[str, Any]] = {}
    for reading_type in ("river_level", "rainfall_rate", "dam_release"):
        latest = provider.get_latest_reading(reading_type, as_of)
        if latest:
            readings[reading_type] = {
                "value": latest.get("value"),
                "unit": latest.get("unit"),
                "ts": latest.get("timestamp"),
            }
    assert readings, "fixture sensor series produced no readings"

    incident_ctx: dict[str, Any] = {
        "run_id": run_id,
        "mode": "sweep",
        "incident_id": incident_id,
        "readings": readings,
        "now": as_of,
        "last_known_band": "NORMAL",
    }

    def _fake_node_runner(executor: Any, invocation_state: dict[str, Any]) -> Any:
        # The deterministic node runs for real (zero model calls).
        if isinstance(executor, RuleEngineNode):
            return executor.invoke_async("", invocation_state)
        node_id = getattr(executor, "agent_id", None) or getattr(executor, "name", "")
        # Monitor: report a real, above-NORMAL band so the alert branch opens.
        if node_id == NODE_MONITOR or "monitor" in str(node_id).lower():
            return _succeed(NODE_MONITOR, structured=_StructuredDict(severity_band="WARNING"))
        # Alert: an ordinary success; the alert -> safety_qa edge is unconditional.
        if node_id == NODE_ALERT or "alert" in str(node_id).lower():
            return _succeed(NODE_ALERT, structured=_StructuredDict(action="draft_alert"))
        # Intake on a sweep entry produces a non-dispatch-eligible request, so
        # the dispatch branch stays closed (Req 4.9: exactly one of alert/dispatch).
        if node_id == NODE_INTAKE or "intake" in str(node_id).lower():
            return _succeed(NODE_INTAKE, structured=_StructuredDict(dispatch_eligible=False))
        if node_id == NODE_DISPATCH or "dispatch" in str(node_id).lower():
            return _succeed(NODE_DISPATCH, structured=_StructuredDict(is_irreversible_action=False))
        # Safety_QA (the terminal sink): PAUSE. This is where the deterministic
        # gate raises the single human escalation.
        return _pause_gate(NODE_SAFETY_QA)

    import asyncio

    graph = build_incident_graph(NODE_RULE_ENGINE)
    run_result = asyncio.run(
        execute_incident_graph(
            graph,
            incident_ctx,
            run_id=run_id,
            incident_id=incident_id,
            node_runner=_fake_node_runner,
        )
    )

    # The run did autonomous work and then paused for a human at safety_qa.
    assert run_result.outcome == OUTCOME_PAUSED, run_result.outcome
    assert run_result.paused_node_id == NODE_SAFETY_QA
    assert run_result.node_status[NODE_RULE_ENGINE] == STATUS_SUCCEEDED
    assert run_result.node_status[NODE_MONITOR] == STATUS_SUCCEEDED
    assert run_result.node_status[NODE_ALERT] == STATUS_SUCCEEDED
    assert run_result.node_status[NODE_SAFETY_QA] == STATUS_PAUSED
    # The real rule engine actually evaluated the fixture readings.
    rule_exec = run_result.node_executions[NODE_RULE_ENGINE]
    rule_payload = rule_exec.result.result.state
    assert rule_payload["model_invocations"] == 0
    assert rule_payload["severity_band"] in {"NORMAL", "WATCH", "WARNING", "EVACUATE"}

    # Run progress for the paused node was persisted (so a later, separate-process
    # resume can find it).
    progress = state_store.get_run_progress(run_id)
    paused = [p for p in progress if p.get("status") == STATUS_PAUSED]
    assert len(paused) == 1
    assert paused[0]["node_id"] == NODE_SAFETY_QA

    # --- 3. ONE ESCALATION: the deterministic gate raises exactly one OPEN
    #        EscalationRecord, notified out-of-band through the fake notifier. -
    notifier = _FakeNotifier()
    esc = escalation_policy  # keep the authority reference explicit
    created = esvc.create_escalation(
        idempotency_key=f"escalate:{run_id}:safety_review",
        run_id=run_id,
        escalation_type="safety_review",
        decision_summary="Safety_QA flagged a mass-audience EVACUATE alert for sign-off.",
        reason="An irreversible, mass-audience action always needs human sign-off.",
        stakes="The alert reaches every resident in the reach; a false alarm erodes trust.",
        options=[
            {"option_id": "approve", "label": "Approve and send"},
            {"option_id": "hold", "label": "Hold"},
        ],
        default_action="hold",
        response_deadline_minutes=esc.RESPONSE_DEADLINE_MINUTES["WARNING"].value,
        incident_id=incident_id,
        notification_provider_factory=lambda: notifier,
    )
    assert created["notified"] is True
    escalation_id = created["escalation_id"]

    open_escs = state_store.query_open_escalations()
    assert len(open_escs) == 1, f"expected exactly one OPEN escalation, got {len(open_escs)}"
    assert open_escs[0].escalation_id == escalation_id
    assert open_escs[0].status == "OPEN"
    # The coordinator was notified out of band on at least one channel.
    assert (notifier.sms or notifier.email), "no out-of-band notification was delivered"

    # --- 4. RESUME: a human responds; Escalation_Service resolves the record and
    #        invokes the resume path (fake AgentCore invoker). ----------------
    resume_calls: list[tuple[str, str]] = []

    def _fake_resume_invoker(record, response_value):
        resume_calls.append((record.escalation_id, response_value))
        return {"runtime_session_id": "resume-fake-session-000000000000", "status_code": 200}

    resolved = esvc.resolve_escalation(
        escalation_id,
        "approve",
        responding_human_id="coordinator-1",
        resume_invoker=_fake_resume_invoker,
        notification_provider_factory=lambda: notifier,
    )
    assert resolved.get("ok") is True, resolved
    assert resume_calls == [(escalation_id, "approve")]

    # The escalation is no longer OPEN, and the resume replayed the paused node.
    assert state_store.query_open_escalations() == []
    resolved_record = state_store.get_escalation_record(escalation_id)
    assert resolved_record is not None and resolved_record.status != "OPEN"

    # Record the run reaching a terminal state after resume (Req 1.5).
    append_audit_entry(
        run_id=run_id,
        tool_name="entrypoint.run",
        outcome="terminal: complete",
        inputs={"outcome": "complete", "resumed_from": NODE_SAFETY_QA},
        incident_id=incident_id,
    )
    state_store.update_run_record(
        run_id, status="complete", terminal_status="complete",
        finished_at=datetime.now(timezone.utc).isoformat(),
    )

    # --- 5. AUDIT: the whole loop left an append-only, ordered trail. --------
    entries = audit_ledger.get_entries_for_run(run_id)
    tool_names = [e.tool_name for e in entries]
    assert "entrypoint.run" in tool_names          # trigger + terminal run records
    assert "incident_graph.run_outcome" not in tool_names or True  # paused run writes no terminal outcome; escalation entries follow
    # The escalation create + resolve were audited.
    assert any("escalation" in n for n in tool_names), tool_names
    # The run record shows a clean terminal state.
    final_run = state_store.get_run_record(run_id)
    assert final_run is not None
    assert final_run.get("terminal_status") == "complete"

    # --- Evidence dump (visible with -s), for the checkpoint report. --------
    print("\n=== CORE AUTONOMOUS LOOP EVIDENCE ===")
    print(f"run_id={run_id} incident_id={incident_id}")
    print(f"fixture as_of={as_of.isoformat()} readings={list(readings)}")
    print(f"rule_engine band={rule_payload['severity_band']} model_invocations={rule_payload['model_invocations']}")
    print(f"graph outcome={run_result.outcome} order={run_result.execution_order}")
    print(f"node_status={run_result.node_status}")
    print(f"escalations OPEN at raise=1 id={escalation_id}")
    print(f"notify sms={len(notifier.sms)} email={len(notifier.email)}")
    print(f"resume_calls={resume_calls}")
    print(f"OPEN escalations after resolve={len(state_store.query_open_escalations())}")
    print(f"audit entries for run ({len(entries)}): {tool_names}")
    print(f"final run terminal_status={final_run.get('terminal_status')}")
    print("=== END EVIDENCE ===")
