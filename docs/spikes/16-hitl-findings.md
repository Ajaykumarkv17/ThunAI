# Spike 16.1 — Confirm `HumanInTheLoop` cross-process resume with a custom classifier

**Task:** 16.1 (parent 16, `Escalation_Service` and HITL wiring)
**Requirements:** 11.4, 11.5 · **Design:** Design Question 2 & 3, Open Question 2
**Verification source:** Strands Agents docs MCP server (`strands-agents` power, live `strandsagents.com`
API reference) plus the `strands-agents/docs` GitHub source (`docs/user-guide/concepts/interrupts.md`,
`site/.../agents/interventions.mdx`) via Ref. Sources cited inline below.

> **Rule applied:** where `design.md` and the live docs disagree, the docs win. No deviation was found —
> the design's assumption is confirmed as designed.

---

## 1. What was verified

**`HumanInTheLoop.__init__` signature** (`strands.vended_interventions.hitl.hitl` API reference):

```python
def __init__(*,
             allowed_tools: list[str] | None = None,
             classifier: bool | LLMClassifierConfig | HumanInTheLoopClassifier | None = None,
             enable_trust: bool = False,
             evaluate_trust: EvaluateCallback | None = None,
             evaluate: EvaluateCallback | None = None,
             ask: AskCallback | Literal["stdio"] | None = None) -> None
```

- `classifier` explicitly accepts **a custom callable** implementing the `HumanInTheLoopClassifier`
  Protocol (`__call__(event: BeforeToolCallEvent, **kwargs) -> ClassifierResult | Awaitable[...]`), not
  only the built-in LLM classifier (`classifier=True` / `LLMClassifierConfig`). This matches the
  design's `classifier=deterministic_classifier` usage (§ Design Question 3) exactly — the design never
  uses the LLM-judged mode.
- `ask` is **omitted** in the design's usage (no inline `ask=` kwarg passed). Per the docs: "Omitted
  (default): uses interrupt/resume — agent pauses, caller resumes with response." This is the code path
  the design and Req 11.4/11.5 depend on.
- `before_tool_call` (the handler's hook implementation) returns `Proceed` if the tool is allow-listed,
  trusted, or approved inline; **otherwise a `Confirm` action — "pausing the agent via interrupt when no
  `ask` is set."** `classifier` only changes *whether* `Confirm` is returned (the gating decision); it
  has no effect on *how* the pause/resume plumbing works once `Confirm` is chosen. This is the key
  finding: **the classifier and the interrupt/resume mechanics are orthogonal** — `HumanInTheLoop` always
  uses the SDK's one interrupt/resume implementation regardless of which `classifier` mode gates it.

**Interrupt/resume + cross-process session persistence** (`docs/user-guide/concepts/interrupts.md`,
"Session Management" section):

- The documented pattern is literally a `server()` function (constructs a **fresh** `Agent` with
  `session_manager=FileSessionManager(session_id=..., storage_dir=...)`) called from a separate
  `client()` function that loops: call `server(prompt)` → if `result.stop_reason == "interrupt"`, collect
  `[{"interruptResponse": {"interruptId": interrupt.id, "response": ...}}]` for each
  `result.interrupts[...]`, and calls `server(responses)` again. Each call to `server()` in that loop
  constructs a **new `Agent` object** (same static config, e.g. `Agent(hooks=..., session_manager=...,
  tools=..., ...)`) — the session manager, not object identity, is what makes resume possible. This is
  exactly the shape Req 11.5 needs: "supplies the persisted `run_id`/`incident_id` and the `interruptId`
  + response... reconstructs the paused node (a fresh Strands `Agent` object... built from the same
  static config every time)."
- `session_manager` is documented as automatically persisting "the agent interrupt state between tear
  down and start up" — i.e. across process boundaries, since tear-down/start-up implies no shared memory.
- `_InterruptState.to_dict()`/`from_dict()` (API reference, `strands.interrupt`) confirm interrupt state,
  including unresolved interrupts, is fully serializable — the mechanism session managers rely on.
- `Interrupt.id` (dataclass field, `strands.interrupt.Interrupt`) is the same `id` surfaced as
  `result.interrupts[0].id`, and is the value threaded into `interruptResponse.interruptId` on resume —
  confirming the exact `result.interrupts[0].id` → `[{"interruptResponse": {"interruptId": ...}}]`
  round-trip the design and Open Question 2 asked about.
- The interrupt system is intervention-agnostic: `HumanInTheLoop` is one `InterventionHandler`
  implementation among several (see `interventions.mdx` "Lifecycle Methods" — `beforeToolCall` supports a
  `Confirm` action described as "Pauses agent via interrupt/resume for human approval"). The
  session-management example in the docs uses a bespoke `ApprovalHook`/`BeforeToolCallEvent` handler
  rather than `HumanInTheLoop` verbatim, but `HumanInTheLoop.before_tool_call` is documented to return
  that same `Confirm` action via the same underlying interrupt primitive — there is only one interrupt/
  resume implementation in the SDK, and every intervention handler (including `HumanInTheLoop`) goes
  through it. No separate, HITL-specific resume mechanism exists that could diverge from the documented
  cross-process contract.

## 2. Conclusion — no deviation, design assumption holds

**`HumanInTheLoop(classifier=<callable>)` composes with interrupt/resume across a process boundary as
the design assumes.** Specifically:

- A custom `classifier` callable is first-class, documented API — not a misuse of the LLM-judged mode.
- The classifier only gates *whether* a pause happens; the pause/resume mechanics (`stop_reason ==
  "interrupt"`, `result.interrupts[0].id`, `[{"interruptResponse": {"interruptId": ..., "response":
  ...}}]`) are identical regardless of classifier, and are documented as working end-to-end from a
  **freshly constructed `Agent`** in what the docs' own example structures as separate server/client
  calls — the same shape as separate process invocations, backed by a session manager rather than
  in-memory state.

## 3. Decision

**Keep the design as specified — no fallback needed.** Task 16.2 should wire
`HumanInTheLoop(allowed_tools=..., classifier=deterministic_classifier)` exactly per design §/Design
Question 3, and 16.3's resume path should use the SDK's native `agent([{"interruptResponse":
{"interruptId": ..., "response": <option>}}])` replay for any node that is a genuine Strands `Agent`
using `HumanInTheLoop`, per design §3.2's "replays ... if that node used `HumanInTheLoop` interrupt/
resume" branch.

The **other** pause path in the design — the deterministic gate calling `create_escalation(...)`
directly with **no SDK interrupt object at all** (for `Rule_Engine`, which never runs inside an `Agent`
loop) — is unaffected by this finding and remains exactly as specified; it was never the subject of
Open Question 2 and needed no fallback to begin with.

One caveat worth flagging for 16.3's own verify-first step: this spike did not execute code (no live
Bedrock/agent run), it verified the documented contract only. 16.3's own "verify first" pass should do a
minimal live smoke test (a two-invocation interrupt → resume round trip on a toy tool) before relying on
this for the demo's critical path, consistent with the "docs win, but verify against the real API
surface" rule already applied throughout this task list.

## 4. Implication for 16.2 – 16.4

- **16.2** (`deterministic_classifier` + `HumanInTheLoop` wiring): proceed as designed. Implement the
  classifier as a plain callable satisfying `HumanInTheLoopClassifier`'s
  `__call__(event: BeforeToolCallEvent, **kwargs) -> ClassifierResult`, delegating solely to
  `policy.escalation_policy.decide()`. No `ask=` kwarg (must stay on the interrupt/resume default path —
  passing `ask` would switch to inline blocking mode and break the "nothing blocks inside `/invocations`"
  rule from Design Question 2).
- **16.3** (`surface/escalation_service.py` create/resolve/timeout/resume): implement the native
  `result.interrupts[0].id` → persisted `interrupt_id` → `agent([{"interruptResponse": {"interruptId":
  ..., "response": <option>}}])` resume path for `HumanInTheLoop`-paused nodes, alongside the
  no-interrupt-object resume path for deterministic-gate pauses (Rule_Engine), as design §3.2 already
  splits them. Do the minimal live smoke test mentioned in §3 as part of this task's own verify-first
  step.
- **16.4** (templates + `render_template()`): unaffected by this finding — no change.
- **16.6** (integration test — cross-process resume): can proceed testing the **actual SDK interrupt/
  resume path** for a `HumanInTheLoop`-gated node (spawn a second process given only persisted
  `run_id`/`interrupt_id`), not just the deterministic-gate path, since the composition is confirmed.
