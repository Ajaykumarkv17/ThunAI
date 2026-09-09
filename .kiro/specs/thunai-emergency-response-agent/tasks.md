# Implementation Plan: ThunAI Neighbourhood Emergency Response Agent

> ## ⚠️ READ BEFORE STARTING ANY TASK — VERIFY THE LIVE API SURFACE FIRST
>
> **This is not optional and applies to every single task below.** Before writing
> a line of code for any task, first search the web and/or consult current
> documentation to confirm the *live* API surface of whatever that task touches.
> Use, in order of preference:
> - the **Strands Agents docs MCP server** (`strands-agents`) for anything touching
>   `Agent`, `@tool`, `GraphBuilder`, hooks, `HumanInTheLoop`/interventions,
>   structured output, session managers, or `strands-agents-evals`;
> - the **AgentCore MCP server** for `BedrockAgentCoreApp`, AgentCore Runtime /
>   Memory / Gateway, the AgentCore CLI, and `invoke_agent_runtime`;
> - the **AWS documentation MCP server** for DynamoDB, EventBridge Scheduler, API
>   Gateway, SES/SNS, AppSync Events, CloudWatch/ADOT, IAM, and AWS Budgets;
> - **constructs.dev** and the installed `aws-cdk-lib` generated docs for every CDK
>   L1/L2 construct property name (especially `aws_cdk.aws_bedrockagentcore`).
>
> **Why this rule exists:**
> - The Strands Agents SDK ships **weekly**. This design is pinned to
>   `strands-agents==1.54.0` / `strands-agents-tools==0.8.7` (dated 2026-08-27) but
>   the SDK may have moved by the time you implement. Confirm the pin still exists
>   and the API still matches before relying on it.
> - The **AgentCore CLI and SDK are under active development**, and the design
>   explicitly notes the older `bedrock-agentcore-starter-toolkit` is **deprecated**
>   (uninstall it if present). Confirm the current CLI/SDK surface.
> - **When `design.md` and the current docs disagree, the docs win.** If a verified
>   API differs from what the design assumes, follow the docs and note the deviation
>   in the task's implementation notes and (if it changes behaviour) flag it for the
>   design to be updated.
>
> Every task below restates this as its **first checklist line**, tailored to what
> that task touches. Do not skip it, even when the task looks like "pure Python" —
> Pydantic, boto3, and Hypothesis evolve too.

## Overview

This plan converts the ThunAI design into a sequence of incremental, dependency-ordered,
test-driven coding tasks for a code-generation agent. Each task builds on the previous
ones and ends by wiring its output into the growing system — there is no orphaned code.
Tasks are grouped into epics; every leaf sub-task references the specific requirement
clause(s) it satisfies and the design section(s) it implements.

The build proceeds bottom-up along the design's own dependency order:
**schemas & policy → deterministic `Rule_Engine` → state/audit/harness → integration seam &
fixtures → tools → specialist agents → `Incident_Graph` orchestration → `Escalation_Service`
& HITL → AgentCore entrypoint (core autonomous loop assembled here) → memory → triggers →
CDK stacks (Data → Auth → AgentCore → Triggers → Escalation → Realtime → Observability →
Frontend) → the three frontend surfaces → evals → demo/CI/submission artefacts.**

Conventions used below:
- `- [ ]` is a required implementation task. `- [ ]*` is an optional sub-task the executing
  agent must **not** auto-implement (test sub-tasks, and a few clearly-labelled scoring-bonus
  items). See Notes for the required-vs-optional and core-demo-loop guidance.
- Each of the 20 design correctness properties maps to at least one explicit `*` property-test
  task, annotated with its **Property number** and the module under test named in `design.md`.
- Three tasks touch APIs flagged in `design.md` "Open Questions" (Strands `Graph` native
  pause/resume, `HumanInTheLoop` cross-process resume with a custom classifier, `GraphBuilder`
  conditional-edge signature). Those tasks open with a **spike** step and state the fallback
  (the custom `execute_incident_graph` driver already specified in design §3.2 / Design Question 2).

## Tasks

- [ ] 1. Project scaffolding, pinned dependencies, and licence
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 1.1 Create the repository directory structure and pinned Python/JS manifests
    - Verify current published versions of `strands-agents`, `strands-agents-tools`,
      `bedrock-agentcore`, `pydantic`, `hypothesis`, `boto3`, `aws-cdk-lib` against the
      package indexes / docs MCP servers before pinning — confirm the design's pins still exist.
    - Create the folder tree from design "Repository layout" (`agents/`, `tools/`, `harness/`,
      `policy/`, `schemas/`, `integrations/`, `memory/`, `surface/`, `triggers/`, `infra/`,
      `frontend/`, `fixtures/`, `evals/`, `tests/`, `scripts/`) with package `__init__.py` files.
    - Write `requirements.txt` / `pyproject.toml` with **exact** pins (no ranges) and a pinned
      `frontend/package.json`.
    - _Requirements: 19.8_
    - _Design: Repository layout_
  - [ ] 1.2 Create `.env.example`, `.gitignore`, and config-var inventory
    - Verify the full set of env vars every module reads (model ids, region, thresholds,
      `USE_FAKES`, fixture paths, gateway/target names) against the design's config modules.
    - `.env.example` lists **every** variable with empty/synthetic values, `USE_FAKES=1` by default.
    - `.gitignore` excludes `.env`, `.venv`, `__pycache__`, `sessions/`, `node_modules`, `dist/`.
    - _Requirements: 19.6, 18.4_
    - _Design: §3.7, §3.11, Cost Model_
  - [ ] 1.3 Add the MIT `LICENSE` file
    - Confirm the current canonical MIT licence text; commit it unmodified as `LICENSE`.
    - _Requirements: 22.1, 22.2_
    - _Design: Repository layout_

- [ ] 2. Typed decision schemas, entity models, and request lifecycle
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 2.1 Implement `schemas/decisions.py`
    - Verify current Pydantic v2 API (`Field` constraints, `Literal`, `model_validate`/`model_dump`)
      against the docs before writing models.
    - Implement `HazardAssessment`, `EmergencyRequest`, `DispatchDecision`, `AlertDraft`,
      `SafetyReview`; every model carries `confidence: float` in `[0,1]`, a closed `action`
      enum including `"ask_human"`, plus `category` and `is_irreversible_action`.
    - _Requirements: 10.3, 3.2, 5.1, 6.2, 7.1, 9.1_
    - _Design: §4.1_
  - [ ] 2.2 Implement `schemas/entities.py`
    - Verify Pydantic v2 constraints for the entity fields (e.g. `available_capacity >= 0`).
    - Implement `Incident`, `Responder`, `Shelter`, `EscalationRecord`, `EscalationOption`,
      `AuditEntry`, including `EscalationRecord.template_fields()` returning persisted-only values.
    - _Requirements: 15.1, 11.1, 12.4_
    - _Design: §4.2_
  - [ ] 2.3 Implement `schemas/lifecycle.py`
    - Verify Python typing/enum patterns for the transition table (pure Python; no external SDK).
    - Implement `REQUEST_TRANSITIONS` and `validate_transition()` raising `InvalidTransitionError`.
    - _Requirements: 15.7, 13.9_
    - _Design: §4.3_
  - [ ] 2.4 Implement `schemas/structured.py::safe_structured()` validation-failure guard
    - Verify current Strands `structured_output_model` behaviour and the Pydantic `ValidationError`
      shape against the docs before writing the guard.
    - On validation failure synthesise `action="ask_human"`, `confidence=0.0`,
      `category="validation_failure"` and surface the raw output for auditing.
    - _Requirements: 10.11_
    - _Design: §4.1 (validation failure handling)_
  - [ ]* 2.5 Property test — Property 9: decision-model schema round-trip & field completeness
    - Verify the Hypothesis + Pydantic integration approach (`hypothesis.extra.pydantic` vs hand
      `st.builds`) per design Open Question 5 before writing generators.
    - **Property 9** over `schemas/decisions.py`; include `confidence` 0.0/1.0, absent optionals,
      max-length text fields.
    - _Requirements: 3.2, 5.1, 6.2, 7.1, 9.1, 10.3, 21.7_
  - [ ]* 2.6 Property test — Property 14: request lifecycle transitions
    - Verify Hypothesis `stateful.RuleBasedStateMachine` API before writing the machine.
    - **Property 14** over `schemas/lifecycle.py::validate_transition`; 1–50 events incl. invalid targets.
    - _Requirements: 13.9, 15.7, 21.8_
  - [ ]* 2.7 Unit tests for `safe_structured()` validation-failure path
    - Confirm Pydantic error surface; assert the synthesised ask-human decision and audit capture.
    - _Requirements: 10.11_

- [ ] 3. Escalation policy and rule-set configuration (the single autonomy authority)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 3.1 Implement `policy/escalation_policy.py`
    - Verify Pydantic/dataclass import needs for the decision types; module is otherwise pure Python.
    - Implement `PolicyEntry` table, `CONFIDENCE_FLOOR`, thresholds, always/never-ask sets,
      per-band `RESPONSE_DEADLINE_MINUTES`, `DEFAULT_ACTION`, `POLICY_VERSION` content hash,
      `validate_policy()`, and the single `decide()` authority.
    - _Requirements: 10.1, 10.2, 10.4, 10.5, 10.6, 10.7, 10.8, 10.10, 10.12_
    - _Design: §3.4, Design Question 3_
  - [ ] 3.2 Implement `policy/rule_engine_rules.py`
    - Confirm `hashlib` usage; pure Python threshold table.
    - Implement `THRESHOLDS` (river m, rainfall mm/h, dam m³/s), `STALENESS_LIMIT_S`, and a
      content-hash `RULE_SET_VERSION` that changes iff any threshold changes.
    - _Requirements: 2.1, 2.7_
    - _Design: §3.3_
  - [ ] 3.3 Implement `policy/safety_policy.py`
    - Confirm PII regex approach; pure Python.
    - Implement safety policy identifiers, version, and PII/secret detection patterns for `Safety_QA_Agent`.
    - _Requirements: 9.3, 9.8_
    - _Design: §3.1 (Safety_QA), §3.6 (redaction field list)_
  - [ ]* 3.4 Property test — Property 6: escalation decision-table correctness
    - Verify Hypothesis strategy composition; exhaustive driver combinations incl. floor boundary.
    - **Property 6** over `policy/escalation_policy.py::decide`.
    - _Requirements: 5.4, 5.5, 6.3, 6.5, 7.6, 9.2, 10.4, 10.5, 10.6, 10.7, 21.9_
  - [ ]* 3.5 Property test — Property 7: policy validation catches defects
    - **Property 7** over `validate_policy` + `decide` (defect ⇒ global `ASK_HUMAN`).
    - _Requirements: 10.10_
  - [ ]* 3.6 Property test — Property 8: policy version changes iff any entry changes
    - **Property 8** over the `POLICY_VERSION` hash function.
    - _Requirements: 10.2_

- [ ] 4. Deterministic `Rule_Engine` graph node (zero model calls)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 4.1 Implement `agents/rule_engine.py::RuleEngineNode`
    - Verify current Strands `MultiAgentBase`, `MultiAgentResult`, `NodeResult`, `Status`,
      `AgentResult`, `Message`/`ContentBlock` API against the Strands docs MCP server before writing.
    - Deterministic severity banding (highest band wins), per-reading availability/staleness/range
      handling, total-outage `unverified` behaviour, `model_invocations=0`, and node result payload.
    - _Requirements: 2.1, 2.2, 2.3, 2.5, 2.6, 2.9, 2.10_
    - _Design: §3.3_
  - [ ]* 4.2 Property test — Property 1: `Rule_Engine` determinism
    - **Property 1** over `RuleEngineNode` (identical inputs ⇒ identical band & triggered rules).
    - _Requirements: 2.4_
  - [ ]* 4.3 Property test — Property 4: `Rule_Engine` monotonicity
    - **Property 4** over `RuleEngineNode`; generate boundary values, ±1 neighbours, min/max per Req 21.3.
    - _Requirements: 2.8, 21.3_
  - [ ]* 4.4 Property test — Property 5: reading unavailability & degraded evaluation
    - **Property 5** over `RuleEngineNode` (each unavailability cause; no decrease on total outage).
    - _Requirements: 2.5, 2.9_

- [ ] 5. Checkpoint — foundations
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - Ensure all schema, policy, and rule-engine tests pass; ask the user if questions arise.

- [ ] 6. Shared live state & DynamoDB access layer
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 6.1 Implement `memory/state_store.py`
    - Verify current boto3 DynamoDB `update_item`/`put_item` `ConditionExpression` semantics,
      `ConditionalCheckFailedException`, and TTL behaviour against the AWS docs MCP server.
    - Implement `idempotent_write` (missing-key rejection, 72h replay), `assign_responder_atomic`
      and `decrement_shelter_capacity` (single-item conditional updates), entity CRUD with
      last-write-wins reads, GSI query helpers, processed-trigger-event records, run-progress
      persistence, and the 3-failure/30s write-failure escalation hook point.
    - _Requirements: 15.1, 15.2, 15.3, 15.4, 15.5, 15.8, 15.9, 15.10, 15.11, 6.4, 6.7, 6.10_
    - _Design: §4.4_
  - [ ]* 6.2 Property test — Property 2: idempotent write replay
    - **Property 2** over `state_store.py::idempotent_write` across all registered write functions.
    - _Requirements: 5.9, 6.9, 7.10, 11.7, 15.2, 21.4_
  - [ ]* 6.3 Property test — Property 12: no double-assignment
    - Verify Hypothesis stateful machine API; **Property 12** over `assign_responder_atomic` with racing sequences.
    - _Requirements: 6.4, 6.10, 21.5_
  - [ ]* 6.4 Property test — Property 13: shelter capacity invariant
    - **Property 13** over `decrement_shelter_capacity` incl. over-capacity/over-release attempts.
    - _Requirements: 6.7, 15.8, 21.6_
  - [ ]* 6.5 Property test — Property 19: state-write-visible-on-read (last-write-wins)
    - **Property 19** over `state_store.py`.
    - _Requirements: 15.1_

- [ ] 7. Append-only Audit Ledger
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 7.1 Implement `memory/audit_ledger.py` + `harness/audit.py::append_audit_entry`
    - Verify boto3 DynamoDB `put_item` with `attribute_not_exists` condition (append-only) against
      the AWS docs MCP server; confirm no `update_item`/`delete_item` code path exists.
    - _Requirements: 15.6, 16.8_
    - _Design: §4.4, §3.6_
  - [ ]* 7.2 Property test — Property 3: audit-ledger append-only invariant
    - **Property 3** over `harness/audit.py` + `memory/audit_ledger.py` (updates/deletes rejected).
    - _Requirements: 15.6_

- [ ] 8. Reliability Harness (redaction + lifecycle hooks)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 8.1 Implement `harness/redaction.py::redact_fields()`
    - Confirm the configured PII/secret field-name list from `policy/safety_policy.py`; pure Python recursion.
    - _Requirements: 16.10, 20.2_
    - _Design: §3.6_
  - [ ] 8.2 Implement `harness/hooks.py` (all hook classes in one module)
    - Verify current Strands hooks API against the docs MCP server: `HookProvider`, `HookRegistry`,
      `BeforeToolCallEvent`, `AfterToolCallEvent`, `BeforeModelCallEvent`, `event.cancel_tool`,
      `event.cancel_model_call`, and how `run_id`/token counts are exposed on events.
    - Implement `ApprovalGateHook`, `ToolCallCapHook`, `SpendCapHook`, `NotificationCapHook`,
      `AuditHook` (block write on audit-append failure), and the `WRITE_TOOLS` allowlist.
    - _Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.7, 16.8, 16.9_
    - _Design: §3.6_
  - [ ]* 8.3 Property test — Property 15: redaction completeness
    - **Property 15** over `harness/redaction.py` (nested dicts, both call sites).
    - _Requirements: 16.10, 20.2_
  - [ ]* 8.4 Property test — Property 17: deterministic Harness enforcement
    - **Property 17** over the four hook classes (identical inputs ⇒ identical decisions).
    - _Requirements: 16.2, 16.4, 16.5, 16.6, 16.11_
  - [ ]* 8.5 Property test — Property 18: cap enforcement blocks and records
    - **Property 18** over `harness/hooks.py` (caps block, one breach entry, run outcome `partial`).
    - _Requirements: 16.4, 16.5, 16.6, 16.7_

- [ ] 9. Checkpoint — state, audit, and harness
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - Ensure all state/audit/harness tests pass; ask the user if questions arise.

- [ ] 10. Integration seam — fixture and live backends
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 10.1 Implement `integrations/sensor_provider.py`
    - Verify AgentCore Gateway tool-prefix convention (`${target}___${tool}`) for `LiveSensorProvider`
      against the AgentCore MCP server; `FixtureSensorProvider` reads `fixtures/hazard_readings/`.
    - `SensorProvider` Protocol + Fixture + Live implementations.
    - _Requirements: 19.5, 3.1_
    - _Design: §3.7_
  - [ ] 10.2 Implement `integrations/notification_provider.py`
    - Verify current boto3 SNS (SMS) and SES send APIs against the AWS docs MCP server for the live backend.
    - `NotificationProvider` Protocol + `FixtureNotifier` + `SnsSesNotifier`.
    - _Requirements: 11.2_
    - _Design: §3.7_
  - [ ] 10.3 Implement `integrations/knowledge_provider.py`
    - Verify the current Bedrock Knowledge Base retrieve integration path (direct
      `bedrock-agent-runtime` boto3 client vs deprecated `retrieve` tool) per design Open Question 6.
    - `KnowledgeProvider` Protocol + `FixtureKnowledge` + `BedrockKBKnowledge`.
    - _Requirements: 8.1_
    - _Design: §3.7, Open Question 6_
  - [ ] 10.4 Implement `integrations/__init__.py` backend selector + credential-absent abort
    - Confirm `USE_FAKES` env resolution and the credential-absent abort-before-first-call behaviour.
    - _Requirements: 19.5, 19.13, 18.10_
    - _Design: §3.7_

- [ ] 11. Fixtures & seed data (synthetic only)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 11.1 Create `fixtures/seed_dataset.json`
    - Confirm entity field shapes against `schemas/entities.py`; all data synthetic, matching the
      data-minimisation field list.
    - 14 responders, 3 shelters (capacity 120), ~900 aggregate resident points.
    - _Requirements: 18.1, 18.2_
    - _Design: §3.9_
  - [ ] 11.2 Create `fixtures/hazard_readings/` 35-day synthetic series
    - 30-day baseline + 5-day rising-river demo scenario per reading type.
    - _Requirements: 3.1, 17.3_
    - _Design: §3.9_
  - [ ] 11.3 Create `fixtures/sample_messages/` multilingual resident messages
    - 6–8 Tamil/English messages spanning categories, one mobility-assistance, one ambiguous-location,
      one off-topic (manual triage).
    - _Requirements: 21.1, 5.6, 5.8_
    - _Design: §3.9_
  - [ ] 11.4 Implement `scripts/seed.sh` (idempotent reset)
    - Confirm shell/boto3 approach; one command, no interactive input, repeatable to the same state.
    - _Requirements: 19.7_
    - _Design: §3.9_

- [ ] 12. Tools layer
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 12.1 Implement `tools/sensor_tools.py`
    - Verify current Strands `@tool` decorator + docstring contract against the docs MCP server.
    - `get_river_level` / `get_rainfall_rate` / `get_dam_release` over the sensor provider seam.
    - _Requirements: 3.1_
    - _Design: §2.1, §3.7_
  - [ ] 12.2 Implement `tools/incident_tools.py`
    - Verify `@tool` return-shape guidance; wire to `state_store` idempotent writes.
    - `create_or_update_incident` (idempotent update, higher-band wins), baseline reads.
    - _Requirements: 3.3, 3.4, 3.7_
    - _Design: §3.2, §4.4_
  - [ ] 12.3 Implement `tools/intake_tools.py`
    - Verify `@tool` + any image-handling helper; `resolve_location`, `detect_language`, `dedupe_check`.
    - _Requirements: 5.6, 5.7, 5.9_
    - _Design: §3.1 (Intake)_
  - [ ] 12.4 Implement `tools/dispatch_tools.py`
    - `find_candidate_responders` (radius + haversine), `assign_responder` via `assign_responder_atomic`.
    - _Requirements: 6.1, 6.8_
    - _Design: §3.1 (Dispatch), §4.4_
  - [ ] 12.5 Implement `tools/alert_tools.py`
    - `get_channel_limits`, `get_shelter_capacity`, `deliver_alert` (idempotent per incident/channel/lang/band).
    - _Requirements: 7.1, 7.10_
    - _Design: §3.1 (Alert), §4.4_
  - [ ] 12.6 Implement `tools/knowledge_tools.py`
    - Verify the confirmed Bedrock KB retrieve path from 10.3; `retrieve_passages` with relevance scores.
    - _Requirements: 8.1_
    - _Design: §3.1 (Knowledge), Open Question 6_
  - [ ] 12.7 Implement `tools/escalation_tools.py::create_escalation`
    - Called by the deterministic gate (not the model); persists the `EscalationRecord` via `state_store`.
    - _Requirements: 11.1_
    - _Design: §3.4, §3.5_
  - [ ]* 12.8 Property test — Property 11: idempotent incident update
    - **Property 11** over `tools/incident_tools.py::create_or_update_incident` + `idempotent_write`.
    - _Requirements: 3.4_

- [ ] 13. Specialist agents, model routing, and orchestrator
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 13.1 Implement `agents/config.py` (model routing + `validate_config`)
    - Verify current Strands `BedrockModel` provider API and available Amazon Nova model ids
      (Nova Lite / Nova Pro class) against the Strands + AgentCore docs MCP servers.
    - Two env-driven model ids, `MODEL_FOR_ROLE`, cold-start `validate_config()`.
    - _Requirements: 19.9, 20.6_
    - _Design: Cost Model_
  - [ ] 13.2 Implement `agents/monitor_agent.py`
    - Verify current Strands `Agent` constructor API (`system_prompt`, `tools`, `agent_id`,
      `structured_output_model`, `hooks`, `interventions`, no session manager for graph members).
    - Produce `HazardAssessment`; retrieve prior sweep + 30-day baseline; incident create/update/downgrade.
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10_
    - _Design: §3.1 (Monitor)_
  - [ ] 13.3 Implement `agents/intake_agent.py` (+ deterministic post-processing)
    - Verify `Agent` + `image_reader`/image content handling for uploaded hazard images.
    - Produce `EmergencyRequest`; deterministic post-processing forces IMMEDIATE on mobility/medical;
      idempotent per inbound message id; free-text stored separately from typed request.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10_
    - _Design: §3.1 (Intake), §4.1_
  - [ ] 13.4 Implement `agents/dispatch_agent.py`
    - Produce `DispatchDecision`; candidate ranking, no-capacity/shelter-capacity escalation branches,
      re-plan on responder-no-longer-available.
    - _Requirements: 6.1, 6.2, 6.3, 6.5, 6.6, 6.8, 6.10, 6.11_
    - _Design: §3.1 (Dispatch)_
  - [ ] 13.5 Implement `agents/alert_agent.py`
    - Fan-out channel×language `AlertDraft`s, character-limit recomposition, submit-to-Safety_QA,
      EVACUATE/mass-audience escalation, no-capacity guidance text.
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8, 7.9, 7.11_
    - _Design: §3.1 (Alert)_
  - [ ] 13.6 Implement `agents/safety_qa_agent.py`
    - Produce `SafetyReview`; PII/policy checks; review of irreversible actions before execution.
    - _Requirements: 9.1, 9.2, 9.3, 9.5, 9.6, 9.7_
    - _Design: §3.1 (Safety_QA), §3.3 (safety_policy)_
  - [ ] 13.7 Implement `agents/knowledge_agent.py`
    - Grounded, cited answers; language handling; no-answer/relevance-floor routing; advisory disclaimer.
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7, 8.8, 8.9_
    - _Design: §3.1 (Knowledge)_
  - [ ] 13.8 Implement `agents/coordinator_orchestrator.py`
    - Verify the current Strands "agents as tools" pattern and session-manager attachment rules.
    - Read-only specialists wrapped as `@tool`; holds no write tool; unroutable-request behaviour;
      the only agent with a session manager.
    - _Requirements: 4.10, 4.11, 4.12, 4.13, 4.14_
    - _Design: §3.1 (Coordinator_Orchestrator)_
  - [ ]* 13.9 Property test — Property 10: intake conditional field-assignment (deterministic post-processing)
    - **Property 10** over `agents/intake_agent.py` deterministic post-processing (urgency forcing,
      INFORMATION/OTHER rules). Model-judged label check is covered separately in the eval suite.
    - _Requirements: 5.2, 5.3_

- [ ] 14. Checkpoint — agents
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - Ensure all agent unit tests and the intake property test pass; ask the user if questions arise.

- [ ] 15. `Incident_Graph` and the pause-aware driver (SPIKE)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 15.1 Spike: confirm the graph API before building on it
    - **Spike (Open Questions 1 & 3):** verify against the Strands docs MCP server (a) the exact
      `GraphBuilder` API and the `add_edge(..., condition=...)` callable signature and what `ctx`
      exposes, and (b) whether a native `Graph` run can be suspended mid-run and resumed in a
      **different process** with only some nodes interrupt-capable.
    - **Fallback:** if native pause/resume does not hold, implement the custom
      `execute_incident_graph` thin driver exactly as specified in design §3.2 / Design Question 2.
      Record the finding in the task notes.
    - _Requirements: 4.1_
    - _Design: §3.2, Design Question 2, Open Questions 1 & 3_
  - [ ] 15.2 Implement `agents/incident_graph.py::build_incident_graph` (declared topology)
    - Verify confirmed `GraphBuilder` API from 15.1; wire nodes, conditional edges, entry points
      (sweep vs intake), timeouts, and max-node count from one node/edge factory.
    - _Requirements: 4.1, 4.3, 4.4, 4.9_
    - _Design: §3.2_
  - [ ] 15.3 Implement `agents/incident_graph.py::execute_incident_graph` (pause-aware driver)
    - Verify `AgentResult.stop_reason == "interrupt"` handling; implement per-node/overall timeouts,
      status set (succeeded/failed/timed_out/skipped/not_executed/paused), run outcomes, run-progress
      persistence on pause, and branch-continuation on non-terminal failure.
    - _Requirements: 4.2, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9_
    - _Design: §3.2_
  - [ ]* 15.4 Integration test — graph execution order, partial/halted/failed outcomes, skipped branch
    - Assert `execution_order` reproducibility, per-node statuses, and Req 4.6–4.9 outcomes.
    - _Requirements: 4.2, 4.5, 4.6, 4.7, 4.8, 4.9_

- [ ] 16. `Escalation_Service` and HITL wiring (SPIKE)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 16.1 Spike: confirm `HumanInTheLoop` cross-process resume with a custom classifier
    - **Spike (Open Question 2):** verify against the Strands docs MCP server that
      `HumanInTheLoop(classifier=<callable>)` composes with interrupt/resume across a process
      boundary — `result.interrupts[0].id` resumable via `[{"interruptResponse": ...}]` from a
      freshly constructed `Agent` in a new process.
    - **Fallback:** if it does not compose, pause via the deterministic gate calling
      `create_escalation(...)` directly and resume through the driver's recorded-decision path
      (no SDK interrupt object), as design §3.2 / Design Question 3 already specifies.
    - _Requirements: 11.4, 11.5_
    - _Design: Design Question 2 & 3, Open Question 2_
  - [ ] 16.2 Implement `agents/hitl.py::deterministic_classifier` + `HumanInTheLoop` wiring
    - Verify the `ClassifierResult` shape; the classifier delegates solely to
      `policy.escalation_policy.decide()`; `allowed_tools` generated from the tool-registration list.
    - _Requirements: 16.11, 10.4, 10.5, 10.6, 10.7_
    - _Design: Design Question 3_
  - [ ] 16.3 Implement `surface/escalation_service.py` (create/resolve/timeout/resume)
    - Verify `invoke_agent_runtime` (`mode=resume`, `runtimeSessionId` length, `interruptResponse`)
      against the AgentCore MCP server.
    - Persist OPEN records, publish to inbox + out-of-band notify, resolve on matching option,
      timeout→default action, idempotent resolution, invalid-option rejection, delivery-failure
      handling, resume-failure handling.
    - _Requirements: 11.1, 11.2, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 11.10, 11.11, 11.12_
    - _Design: §3.5, Design Question 2_
  - [ ] 16.4 Implement hand-authored templates + `render_template()` in `surface/escalation_service.py`
    - Confirm every interpolated field comes only from the persisted `EscalationRecord` (no model call).
    - _Requirements: 11.3_
    - _Design: §3.5_
  - [ ]* 16.5 Unit tests — resolution idempotence, invalid-option rejection, deadline default, delivery-failure
    - Also contributes to **Property 2** (idempotent resolution) coverage at this call site.
    - _Requirements: 11.6, 11.7, 11.10, 11.11_
  - [ ]* 16.6 Integration test — cross-process resume ("return and persist")
    - Spawn a second process given only persisted `run_id`/`interrupt_id`; assert the run completes.
    - _Requirements: 11.5_

- [ ] 17. AgentCore entrypoint and run lifecycle (core autonomous loop assembled here)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 17.1 Implement `surface/entrypoint.py` (`BedrockAgentCoreApp`, `/invocations` + `/ping`, mode dispatch)
    - Verify current `bedrock_agentcore.runtime.BedrockAgentCoreApp` API and the AgentCore Runtime
      contract (single invocation + health endpoint, port, payload shape) against the AgentCore MCP server.
    - Dispatch `mode` in {sweep, intake, resume, chat}; run `validate_policy()`/`validate_config()` at cold start.
    - _Requirements: 19.3, 19.11, 1.1_
    - _Design: Design Question 1 & 2, §3.4, Cost Model_
  - [ ] 17.2 Implement run-record, failure detection, dedupe, and sweep-skip logic
    - Verify how run/trigger metadata is threaded through the entrypoint; write run/failure/skip/
      duplicate-suppression/rejection records to the ledger and the max-run-duration failure notify.
    - _Requirements: 1.5, 1.6, 1.8, 1.9, 1.10_
    - _Design: Error Handling table_
  - [ ] 17.3 Implement `surface/queries.py` list/read helpers
    - Verify DynamoDB GSI query patterns; helpers for recent-runs, open-incidents, decision-inbox,
      and per-incident audit ordering with declared sort keys and truncation.
    - _Requirements: 1.7, 12.1, 12.4, 12.7, 12.8_
    - _Design: §4.4 (GSIs), §3.8_
  - [ ]* 17.4 Property test — Property 20: list ordering and truncation (surface helpers)
    - **Property 20** over `surface/queries.py` for all four list types (React render is covered in 25.6).
    - _Requirements: 1.7, 12.1, 12.4, 12.7, 12.8_

- [ ] 18. Checkpoint — core autonomous loop (local, `USE_FAKES=1`)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - Ensure the trigger→autonomous-work→one-escalation→resume→audit loop runs end to end on fixtures
    and all tests pass; ask the user if questions arise.

- [ ] 19. Memory across separate runs
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 19.1 Implement `memory/memory_store.py`
    - Verify `AgentCoreMemorySessionManager` / `AgentCoreMemoryConfig` / `RetrievalConfig` and
      `strands_dynamodb_storage.DynamoDBStorage` APIs against the AgentCore + Strands docs MCP servers.
    - Session manager for the orchestrator only; 30-day baseline ring buffer + reading count in state;
      preference persistence/supersession; `retrieve_with_retry`; conversation-manager context budget.
    - _Requirements: 17.1, 17.2, 17.3, 17.4, 17.5, 17.7, 17.10_
    - _Design: §3.10, Design Question 4_
  - [ ]* 19.2 Unit tests — insufficient baseline (<7), absent prior state, concurrent-session reject, preference supersession
    - _Requirements: 17.4, 17.6, 17.8, 17.9_

- [ ] 20. Trigger Lambda handlers
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 20.1 Implement `triggers/sweep_lambda.py`
    - Verify EventBridge Scheduler target payload + `invoke_agent_runtime` from Lambda against the
      AWS + AgentCore docs MCP servers; enforce the in-progress-sweep skip.
    - _Requirements: 1.1, 1.8_
    - _Design: §5.1 (requirements), Architecture diagram_
  - [ ] 20.2 Implement `triggers/ingestion_lambda.py`
    - Verify API Gateway → Lambda event shape and payload validation; message/sensor/image intake,
      ≤10MB + format validation, dedupe within 24h, rejection responses.
    - _Requirements: 1.2, 1.3, 1.4, 1.9, 1.10_
    - _Design: Architecture, Error Handling table_
  - [ ] 20.3 Implement `triggers/timeout_sweeper_lambda.py`
    - Verify EventBridge rule (1-min) invocation; apply default action past deadline via `Escalation_Service`.
    - _Requirements: 11.6_
    - _Design: Design Question 2_
  - [ ] 20.4 Implement `triggers/stream_publisher_lambda.py`
    - Verify DynamoDB Streams → AppSync Events publish API against the AWS docs MCP server; namespaced channels.
    - _Requirements: 12.5, 14.3, 11.2_
    - _Design: §3.8_

- [ ] 21. CDK infrastructure (one Python CDK app)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 21.1 Implement `infra/checks/arch_guard.py`
    - Confirm the ELF `e_machine` header field for AArch64 (0xB7); pure Python file inspection.
    - _Requirements: 19.2_
    - _Design: §3.11_
  - [ ] 21.2 Implement `infra/stacks/data_stack.py`
    - Verify current `aws-cdk-lib` DynamoDB (table + Streams) and S3 L2 construct props via constructs.dev.
    - `thunai-state` (+GSIs, Streams), `thunai-audit`, `thunai-memory`, S3 buckets.
    - _Requirements: 19.1, 15.1, 15.6_
    - _Design: §3.11, §4.4_
  - [ ] 21.3 Implement `infra/stacks/auth_stack.py`
    - Verify Cognito user-pool + groups L2 construct props via constructs.dev.
    - _Requirements: 12.9, 13.6_
    - _Design: §3.8, §3.11_
  - [ ] 21.4 Implement `infra/stacks/agentcore_stack.py` (Runtime, Memory, Gateway, Identity)
    - **Verify `aws_cdk.aws_bedrockagentcore` L1 construct property names** (e.g. `CfnRuntime`
      `networkConfiguration`/`networkMode`, `agentRuntimeArtifact`) against constructs.dev / the
      generated `aws-cdk-lib` docs — per design Open Question 4, casing/nesting can shift.
    - Call `verify_arm64_or_abort()` first; scope one least-privilege execution role.
    - _Requirements: 19.1, 19.2_
    - _Design: §3.11, Open Question 4_
  - [ ] 21.5 Implement `infra/stacks/triggers_stack.py`
    - Verify EventBridge Scheduler + API Gateway L2 construct props via constructs.dev.
    - _Requirements: 1.1, 1.2_
    - _Design: §3.11_
  - [ ] 21.6 Implement `infra/stacks/escalation_stack.py`
    - Verify SNS/SES + Lambda + EventBridge-rule L2 construct props; least-privilege role.
    - _Requirements: 11.2_
    - _Design: §3.11_
  - [ ] 21.7 Implement `infra/stacks/realtime_stack.py` (AppSync Events)
    - **Verify AppSync Events `EventApi` / `ChannelNamespace` per-namespace auth** granularity via
      constructs.dev — public read on `/status/*`, Cognito-gated on `/incidents/*`, `/escalations/*`
      (design Open Question 7); decide single-API vs split-API.
    - _Requirements: 12.5_
    - _Design: §3.8, Open Question 7_
  - [ ] 21.8 Implement `infra/stacks/observability_stack.py`
    - Verify CloudWatch Transaction Search enablement + AWS Budgets L2/L1 props via the AWS docs MCP server.
    - Budget alarm at the configured threshold ($20 backstop).
    - _Requirements: 20.4_
    - _Design: §3.11, Cost Model_
  - [ ] 21.9 Implement `infra/stacks/frontend_stack.py` (Amplify Hosting)
    - Verify Amplify `App`/`Branch` L2 construct props + env-var injection via constructs.dev.
    - _Requirements: 19.1_
    - _Design: §3.8, §3.11_
  - [ ] 21.10 Implement `infra/app.py` (wire all stacks in dependency order)
    - Data → Auth → AgentCore → Triggers → Escalation → Realtime → Observability → Frontend;
      explicit `add_dependency` calls; `cdk deploy --all` with no manual console step.
    - _Requirements: 19.1_
    - _Design: §3.11_
  - [ ]* 21.11 Unit tests — IAM least-privilege (no `Resource: "*"`), arch-guard abort, `cdk synth` snapshot
    - Verify `aws_cdk.assertions.Template` API; assert no wildcard/admin scopes and arm64 abort.
    - _Requirements: 18.3, 19.2_

- [ ] 22. Observability and cost control
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 22.1 Implement OTel/ADOT wiring + per-run metrics recording + budget notification
    - Verify `opentelemetry-instrument`/ADOT distro usage and Strands `result.metrics` fields against
      the Strands + AWS docs MCP servers; emit one trace per run (spans per node/tool/model), redacted
      attributes, token/latency/cost recording exposed to the console; continue run on emission failure.
    - _Requirements: 20.1, 20.2, 20.3, 20.5, 20.7, 20.8, 20.9_
    - _Design: Cost Model, §3.11_
  - [ ]* 22.2 Bonus GenAI Observability dashboards / trace views (OPTIONAL — scoring bonus, not required for the demo loop)
    - Verify CloudWatch GenAI Observability setup; build supplementary dashboards.
    - _Requirements: 20 (bonus)_

- [ ] 23. Frontend shared infrastructure (React + TypeScript + Vite)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 23.1 Implement `frontend/src/shared/auth.ts` + route guard
    - Verify current `aws-amplify/auth` (`signIn`, `fetchAuthSession`) API against Amplify docs;
      client-side group guard backed by server-side re-check.
    - _Requirements: 12.9, 12.10, 13.6_
    - _Design: §3.8_
  - [ ] 23.2 Implement `frontend/src/shared/realtime.ts`
    - Verify current `aws-amplify/data` **events** API (`events.connect`, `channel.subscribe`)
      against Amplify docs; wildcard subscriptions + 15s staleness detection.
    - _Requirements: 12.5, 12.6, 14.9_
    - _Design: §3.8_
  - [ ] 23.3 Implement `frontend/src/shared/i18n.ts`
    - Confirm the i18n keyed-dictionary approach; per-field translation-unavailable fallback indicator.
    - _Requirements: 14.4, 14.10_
    - _Design: §3.8_

- [ ] 24. Resident Status Page (public surface)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 24.1 Implement `frontend/src/resident/` public status page
    - Verify React Router + Amplify data access; severity band, shelter availability, advisory notice,
      i18n, staleness/stale-data indicators, aggregate-only resident info (no PII).
    - _Requirements: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.8, 14.9, 14.10_
    - _Design: §3.8_
  - [ ]* 24.2 Accessibility + i18n render tests
    - Verify `@axe-core/react` API; assert contrast/keyboard/text-alt per language (note: full WCAG
      needs manual assistive-tech testing).
    - _Requirements: 14.7_

- [ ] 25. Coordinator Console (primary human surface)
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 25.1 Implement `frontend/src/coordinator/` Decision_Inbox (one-tap)
    - Verify Amplify data mutation/fetch + `AbortSignal.timeout`; ordered by deadline, per-option
      controls, disable-on-activation, error-and-re-enable on failure, ≤10s recompute.
    - _Requirements: 12.1, 12.2, 12.3_
    - _Design: §3.8_
  - [ ] 25.2 Implement open-incident list + shelter availability view
    - Ordered by severity then recency; open-request/assigned counts; shelter unoccupied/total.
    - _Requirements: 12.4_
    - _Design: §3.8_
  - [ ] 25.3 Implement per-incident audit trail view
    - Oldest→newest; redacted inputs; approver id where applicable.
    - _Requirements: 12.7_
    - _Design: §3.8, §4.4_
  - [ ] 25.4 Implement recent-runs cost/latency view
    - 20 most recent; tokens, latency ms, model id, estimated cost with currency.
    - _Requirements: 12.8, 1.7_
    - _Design: §3.8, Cost Model_
  - [ ] 25.5 Implement the `Coordinator_Orchestrator` conversational panel (secondary surface)
    - Verify chat invocation (`mode=chat`); inbox/incident list remain operable if the panel is unavailable.
    - _Requirements: 12.11_
    - _Design: §3.8_
  - [ ]* 25.6 Accessibility tests + Property 20 (React list rendering)
    - Verify `@axe-core/react`; assert keyboard/focus/contrast and **Property 20** client-side ordering/merge.
    - _Requirements: 12.12, 1.7, 12.1, 12.4, 12.7, 12.8_

- [ ] 26. Responder Interface
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 26.1 Implement `frontend/src/responder/` assignment + status flow
    - Verify Amplify auth/data; assignment fields, state-gated controls (accept/decline/en-route/
      on-scene/completed), decline/non-ack re-dispatch, completion→verification, own-assignments-only auth.
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6, 13.7, 13.8, 13.9, 13.10_
    - _Design: §3.8, §4.3_
  - [ ]* 26.2 Responder interaction + a11y tests
    - Assert invalid-transition rejection messaging and control gating.
    - _Requirements: 13.2, 13.9_

- [ ] 27. Checkpoint — surfaces and deployment
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - Ensure frontend tests pass, `cdk synth` succeeds with the arch guard, and the deployed (or locally
    served) surfaces render seeded data; ask the user if questions arise.

- [ ] 28. Strands Evals scenario suite
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 28.1 Implement the eval harness + 20+ recorded scenarios
    - Verify the current `strands-agents-evals` deterministic-evaluator API (import path, exact-match
      on `structured_output`, trajectory/state assertions) against the Strands docs MCP server
      (design Open Question 10).
    - Coverage: one per severity band, one intake per language, dispatch with/without capacity,
      escalation approve/decline/timeout; each scenario declares id/fixture/expected outcome/pass condition.
    - _Requirements: 21.1, 21.11, 21.12, 21.13_
    - _Design: Testing Strategy, Open Question 10_
  - [ ] 28.2 Implement goal-success computation + README stat wiring
    - `passed/total*100` to one decimal, with total count and Eval_Suite version.
    - _Requirements: 21.2_
    - _Design: Testing Strategy_
  - [ ] 28.3 Implement the single-escalation-per-sweep demo scenario
    - Assert exactly one `Escalation_Record` over the seeded routine sweep.
    - _Requirements: 11.9_
    - _Design: §3.4 (tuning note)_
  - [ ]* 28.4 Eval test — Property 16: grounded output
    - **Property 16** across `Knowledge_Agent` grounding, `render_template()` field grounding, and
      advisory-disclaimer content, via the fixture-backed deterministic evaluator (not raw Hypothesis).
    - _Requirements: 8.3, 11.3, 18.6_
  - [ ]* 28.5 Eval test — Property 10 model-label check (complement to 13.9)
    - Deterministic exact-match of category/urgency against hand-labelled fixture messages.
    - _Requirements: 5.2, 5.3, 21.1_

- [ ] 29. Demo and credential-scan scripts
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 29.1 Implement `scripts/demo.sh`
    - Confirm the one-command, no-interaction contract; run the full trigger→resolution loop on
      fixtures in ≤600s, assert the single-escalation property, exit non-zero naming the first
      incomplete step on failure.
    - _Requirements: 19.12_
    - _Design: §3.9_
  - [ ] 29.2 Implement `scripts/scan_credentials.sh`
    - Verify the current `gitleaks` (or equivalent) CLI invocation; scan working tree + history,
      fail and name the matching path/revision on a hit.
    - _Requirements: 22.10, 22.11_
    - _Design: Testing Strategy (CI)_

- [ ] 30. CI workflow
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 30.1 Implement `.github/workflows/ci.yml`
    - Verify current GitHub Actions syntax and runner setup; run unit/property/integration tests,
      evals, frontend tests + `@axe-core`, `cdk synth`, and the credential scan; on push to default
      and PRs to default; complete within 20 minutes.
    - _Requirements: 21.10, 22.11_
    - _Design: Testing Strategy (CI)_
  - [ ]* 30.2 Credential-scan meta-test
    - Assert the scan step reports and fails on a deliberately fake pattern-matching secret.
    - _Requirements: 22.11_

- [ ] 31. Submission artefacts
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - [ ] 31.1 Write `README.md`
    - Confirm the current Devpost submission field list before finalising claims.
    - Track on line 1 (Good Neighbor Agents); problem/audience/quantified repetition; autonomous
      behaviour; Escalation_Policy thresholds with values; Strands feature-usage table; setup steps
      (clean-clone in ≤15 min); config-variable table; measured mean tokens/latency/cost over ≥10 runs;
      describe only behaviour the code performs, each referencing its AC/eval scenario.
    - _Requirements: 22.3, 22.4, 22.12, 20.10, 19.10, 4.14_
    - _Design: Requirements Traceability_
  - [ ] 31.2 Produce and inline the architecture diagram
    - Export the design's Mermaid architecture diagram to `docs/architecture.png` and inline it;
      show triggers, agent tier + tools, escalation path to human, human-response path back, and
      persisted state; label the escalation arrow with the confidence floor + always-ask categories.
    - _Requirements: 22.5, 22.6, 22.7, 22.8_
    - _Design: Architecture (high-level component diagram)_
  - [ ] 31.3 Write the boundaries and limitations README sections
    - List every Irreversible_Action category, other declined-autonomous actions, each enforcement
      mechanism, and the AC/test verifying it; honest limitations incl. any described-but-unimplemented behaviour.
    - _Requirements: 18.8, 22.13_
    - _Design: §3.4, §3.6, Error Handling_
  - [ ] 31.4 Write the demonstration video script
    - Six ordered beats (trigger, autonomous handling, escalation delivery, approval, resumed
      completion, memory effect), ≥5s each, problem/audience/stakes in the first 40s, total ≤300s.
    - _Requirements: 22.9_
    - _Design: Overview_
  - [ ]* 31.5 Publish the `builder.aws.com` post (OPTIONAL — scoring bonus)
    - "Agents for Humans" in the title; build journey + AWS usage + metrics; link before deadline.
    - _Requirements: (submission bonus)_
  - [ ]* 31.6 Stand up the optional live demo link (OPTIONAL — scoring bonus)
    - Clickable, credential-free, seeded, reset-able URL; re-test the morning of the deadline.
    - _Requirements: 11 (optional live demo), 19.5_

- [ ] 32. Final checkpoint
  > **Verify first (mandatory):** Before implementing this task, consult the current official docs — the Strands Agents docs MCP server, the AgentCore MCP server, the AWS documentation MCP server, and constructs.dev / `aws-cdk-lib` generated docs — plus a web search where useful. Confirm the live API surface, deeply analyse the relevant sections, and reconcile against `design.md`. **When the docs and the design disagree, the docs win**; record any deviation in the task notes.
  - Ensure the full loop succeeds three times in a row on a clean clone, all CI gates pass, no secrets
    are present, and the submission package is consistent with the running system; ask the user if questions arise.

## Notes

- **Core demo loop (required, runs locally with `USE_FAKES=1`)**: Tasks 1–4, 6–8, 10–17, and 29.1
  assemble the judged loop — trigger → autonomous work → one escalation → resume → audit — without any
  third-party credentials. Task 18 is the checkpoint proving it. These are the highest priority.
- **Required for full spec compliance**: Tasks 19–28 and 30–31 implement memory, real triggers, the CDK
  deployment (Req 19), observability (Req 20), the three frontend surfaces (Req 12–14), the eval suite
  (Req 21), CI, and submission artefacts (Req 22). They are required tasks (not `*`) because the
  requirements mandate them, but the demo loop itself does not block on cloud deployment.
- **Optional (`*`) sub-tasks** fall into two groups: (1) all test sub-tasks (unit, property, integration,
  eval, a11y) — marked `*` per the workflow so they can be skipped for a faster MVP, though they are
  strongly recommended and every correctness property depends on them; and (2) three clearly-labelled
  scoring-bonus items (22.2 bonus dashboards, 31.5 builder.aws.com post, 31.6 live demo link). The
  executing agent must **not** auto-implement `*` sub-tasks; the orchestrator offers "required only" vs
  "required + optional."
- **Correctness-property coverage** — every design property maps to an explicit task: P1→4.2, P2→6.2,
  P3→7.2, P4→4.3, P5→4.4, P6→3.4, P7→3.5, P8→3.6, P9→2.5, P10→13.9 (+ eval 28.5), P11→12.8, P12→6.3,
  P13→6.4, P14→2.6, P15→8.3, P16→28.4, P17→8.4, P18→8.5, P19→6.5, P20→17.4 (+ React 25.6).
- **Spikes for design Open Questions** — Task 15.1 (Strands `Graph` native pause/resume + `GraphBuilder`
  conditional-edge signature, Open Questions 1 & 3), Task 16.1 (`HumanInTheLoop` cross-process resume with
  a custom classifier, Open Question 2). Each opens with a verify/spike step and states the design's
  already-specified fallback (the custom `execute_incident_graph` driver, §3.2). Open Questions 4, 6, 7,
  and 10 are folded into the verify-first line of tasks 21.4, 10.3/12.6, 21.7, and 28.1 respectively.
- **API-verification-first is mandatory on every task** (see the banner at the top). When current docs
  disagree with `design.md`, follow the docs and record the deviation.
- This workflow produced planning artefacts only. Implementation begins when you open `tasks.md` and click
  "Start task" on a task item.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0,  "tasks": ["1.1", "1.2", "1.3"] },
    { "id": 1,  "tasks": ["2.1", "2.2", "2.3", "3.2", "3.3", "13.1", "11.2", "11.3"] },
    { "id": 2,  "tasks": ["2.4", "3.1", "4.1", "6.1", "7.1", "8.1", "10.1", "10.2", "10.3", "11.1"] },
    { "id": 3,  "tasks": ["2.5", "2.6", "2.7", "3.4", "3.5", "3.6", "4.2", "4.3", "4.4", "6.2", "6.3", "6.4", "6.5", "7.2", "8.2", "10.4", "11.4"] },
    { "id": 4,  "tasks": ["8.3", "8.4", "8.5", "12.1", "12.2", "12.3", "12.4", "12.5", "12.6", "12.7"] },
    { "id": 5,  "tasks": ["12.8", "13.2", "13.3", "13.4", "13.5", "13.6", "13.7"] },
    { "id": 6,  "tasks": ["13.8", "13.9", "15.1", "16.1", "19.1"] },
    { "id": 7,  "tasks": ["15.2", "16.2", "16.4", "19.2"] },
    { "id": 8,  "tasks": ["15.3", "16.3", "20.4"] },
    { "id": 9,  "tasks": ["15.4", "16.5", "17.1", "17.3"] },
    { "id": 10, "tasks": ["16.6", "17.2", "17.4", "20.1", "20.2", "20.3", "22.1", "23.1", "23.2", "23.3", "29.1", "29.2"] },
    { "id": 11, "tasks": ["21.1", "21.2", "21.3", "24.1", "25.1", "25.2", "25.3", "25.4", "25.5", "26.1", "28.1"] },
    { "id": 12, "tasks": ["21.4", "21.7", "24.2", "25.6", "26.2", "28.2", "28.3", "28.4", "28.5"] },
    { "id": 13, "tasks": ["21.5", "21.6", "21.8", "21.9", "22.2"] },
    { "id": 14, "tasks": ["21.10"] },
    { "id": 15, "tasks": ["21.11", "30.1", "30.2"] },
    { "id": 16, "tasks": ["31.1", "31.2", "31.3", "31.4"] },
    { "id": 17, "tasks": ["31.5", "31.6"] }
  ]
}
```
