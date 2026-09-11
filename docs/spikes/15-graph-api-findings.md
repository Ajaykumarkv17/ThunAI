# Spike 15.1 — Confirm the Strands `Graph` API before building on it

**Task:** 15.1 (parent 15, `Incident_Graph` and the pause-aware driver)
**Requirements:** 4.1 · **Design:** §3.2, Design Question 2, Open Questions 1 & 3
**Verification source:** Strands Agents docs MCP server (`strands-agents` power), current live docs at
`strandsagents.com` — the `strands.multiagent.graph` API reference and the Graph user guide.
API-reference line refs cited below point at `strands-py/src/strands/multiagent/graph.py` on
`harness-sdk@main`, i.e. the current published SDK surface.

> **Rule applied:** where `design.md` and the live docs disagree, the docs win. Two deviations were
> found (Open Questions 1 and 3). Both are recorded here and flagged for `design.md` follow-up.

---

## 1. Confirmed `GraphBuilder` / `Graph` API surface

Building goes through `strands.multiagent.GraphBuilder`, then `.build()` returns a `Graph`. Every
builder method the design assumed exists, with these **confirmed signatures**:

| Design assumed | Confirmed live signature | Verdict |
|---|---|---|
| `GraphBuilder()` | `GraphBuilder.__init__() -> None` | ✅ matches |
| `add_node(executor, "id")` | `add_node(executor: AgentBase \| MultiAgentBase, node_id: str \| None = None) -> GraphNode` | ✅ matches (custom deterministic nodes subclass `MultiAgentBase`) |
| `add_edge("a", "b", condition=...)` | `add_edge(from_node: str \| GraphNode, to_node: str \| GraphNode, condition: EdgeCondition \| None = None) -> GraphEdge` | ✅ matches — but the `condition` **callable signature differs** (see §3) |
| `set_entry_point("id")` | `set_entry_point(node_id: str) -> GraphBuilder` | ✅ matches |
| `set_execution_timeout(600)` | `set_execution_timeout(timeout: float) -> GraphBuilder` | ✅ matches |
| `set_node_timeout(120)` | `set_node_timeout(timeout: float) -> GraphBuilder` | ✅ matches |
| `set_max_node_executions(25)` | `set_max_node_executions(max_executions: int) -> GraphBuilder` | ✅ matches |
| `.build()` | `build() -> Graph` | ✅ matches |

**Additional builder methods available** (not used by the design but relevant to task 15.2/15.3):
`reset_on_revisit(enabled=True)`, `set_graph_id(id)`, `set_session_manager(session_manager)`,
`set_hook_providers(hooks)`, `set_plugins(plugins)`.

**Execution entry points on the built `Graph`:**
- `graph(task, invocation_state=None, **kwargs) -> GraphResult` (sync `__call__`)
- `await graph.invoke_async(task, invocation_state=None, **kwargs) -> GraphResult`
- `graph.stream_async(task, invocation_state=None, **kwargs) -> AsyncIterator[dict]` — yields
  `multi_agent_node_start` / `multi_agent_node_stream` / `multi_agent_node_stop` / `result` events.

**`invocation_state`** is the SDK-native way to pass shared context/config to nodes and to conditions
*without* exposing it to the LLM — the natural carrier for ThunAI's `incident_ctx`.

**Custom deterministic node (the `Rule_Engine` node, §3.3):** confirmed. Subclass
`strands.multiagent.base.MultiAgentBase` and implement
`async def invoke_async(self, task, invocation_state, **kwargs) -> MultiAgentResult`, returning
`MultiAgentResult(status=Status.COMPLETED, results={name: NodeResult(result=AgentResult(...))})`.
`Status` and `NodeResult` live in `strands.multiagent.base`. This confirms the design's "custom graph
node, zero model calls" approach is directly supported.

---

## 2. Timeout & max-node-count configuration surface

All three limits Req 4.4 needs are **first-class on `GraphBuilder`** and are enforced by the SDK's own
executor (they are also constructor args on `Graph.__init__`):

- `set_execution_timeout(timeout: float)` — total run timeout in seconds (`None` = no limit).
- `set_node_timeout(timeout: float)` — per-node timeout in seconds (`None` = no limit).
- `set_max_node_executions(max_executions: int)` — cap on total node executions (`None` = no limit).

`GraphState.should_continue(max_node_executions, execution_timeout) -> (bool, reason)` is the internal
check the SDK uses to stop a run on limit/timeout. So the design's assumption "the driver reads the
timeouts and max-node-count from the same `GraphBuilder` object and enforces them" is achievable either
by letting the SDK enforce them natively, or (in the custom driver) by reading the same values back.

The design's default values (`execution_timeout=600`, `node_timeout=120`, `max_node_executions=25`)
map cleanly onto these three setters with no API friction.

---

## 3. Conditional-edge `condition` callable — DEVIATION (Open Question 3)

**Design assumed:** `condition=lambda ctx: ctx.node_output("monitor").severity_band != "NORMAL"` — i.e.
a `ctx` object exposing a `node_output(node_id)` accessor.

**Docs confirm:** the callable receives a **`GraphState`**, not a `ctx`. Two supported forms:

- **Legacy (state-only):** `Callable[[GraphState], bool]`
- **New-style (with runtime context):** the `EdgeConditionWithContext` protocol —
  `def cond(state: GraphState, *, invocation_state: dict[str, Any], **kwargs) -> bool`.
  The SDK auto-detects which form you passed (via `inspect.signature`) and calls it accordingly, so
  legacy conditions keep working with no migration.

**What `GraphState` actually exposes to a condition** (from the API reference, `graph.py:116`):

- `status` — current graph execution status
- `completed_nodes`, `failed_nodes`, `interrupted_nodes` — sets of node ids
- `execution_order` — list of nodes in execution order
- `results` — **the dict of node results**, keyed by `node_id` → `NodeResult`
  (a node's own output is `state.results["<id>"].result`)
- `task`, `start_time`, `execution_time`
- `should_continue(max_node_executions, execution_timeout)` helper

So the correct way to read a prior node's typed output inside a condition is
**`state.results["monitor"].result`**, not `ctx.node_output("monitor")`.

**Required correction for tasks 15.2 / 15.3 (docs win):**

```python
from strands.multiagent.graph import GraphState

def monitor_raised_incident(state: GraphState) -> bool:
    node = state.results.get("monitor")
    if node is None:
        return False
    return node.result.severity_band != "NORMAL"   # exact attribute access
                                                     # depends on how the Monitor
                                                     # node wraps its structured output

builder.add_edge("monitor", "alert", condition=monitor_raised_incident)
```

Two implementation notes for 15.2:
1. `state.results[id].result` is a `NodeResult.result` (an `AgentResult`/`MultiAgentResult`-shaped
   object). ThunAI's nodes must expose their structured decision on that object in a place the
   condition can read (e.g. attach the parsed Pydantic model), rather than parsing free text — decide
   the exact accessor when the Monitor/Intake/Dispatch nodes are built.
2. **AND vs OR semantics:** in the Python SDK the default is **OR** — a target fires when *any*
   incoming edge's source completes. Req 4.9 ("exactly one of alert vs dispatch reachable") is still
   satisfied by mutually-exclusive `condition=` callables; but if any node ever needs *all* upstream
   deps before firing, use the documented `all_dependencies_complete([...])` AND-condition factory
   pattern rather than assuming AND.

---

## 4. Native cross-process pause/resume — DEVIATION FROM THE OPEN-QUESTION PREMISE (Open Question 1)

**Design premise (Open Question 1 / Design Question 2):** "whether a bare Strands `Graph`'s built-in
execution can itself be suspended mid-run and resumed in a *different process*, with only *some* member
nodes interrupt-capable, is **not confirmed** by the documentation." The design therefore chose a
custom thin driver (`execute_incident_graph`) rather than depend on unverified behaviour.

**Current docs now DO confirm native interrupt + cross-process resume.** The relevant surface:

- `Graph.serialize_state() -> dict[str, Any]` — serialize the current graph state to a plain dict
  (persistable to DynamoDB / any store).
- `Graph.deserialize_state(payload: dict[str, Any]) -> None` — restore state from a persisted dict and
  prepare to resume. The docstring explicitly handles two cases: (1) run ended → resets for
  re-execution; (2) **run was interrupted mid-execution (has `next_nodes_to_execute`) → restores state
  and resumes from the next ready nodes.** This is the exact "suspend mid-run, resume later" contract.
- `GraphBuilder.set_session_manager(SessionManager)` / `Graph(session_manager=...)` — the SDK persists
  graph state and execution history through a session manager, so resume can happen against the same
  persisted session from a **freshly constructed graph object** (i.e. a new process).
- `GraphState.interrupted_nodes` — a first-class set of nodes the user interrupted; and
  `start_time` "resets on each invocation, **even when resuming from interrupt**" — confirming the SDK
  models resume-after-interrupt as a first-class flow.
- `invocation_state` is documented as **"persisted across interrupt/resume cycles (serialized with the
  graph checkpoint)"** — so runtime context survives the pause boundary natively.

The "only *some* nodes interrupt-capable" half is implicitly fine: a node interrupts only if its
executor raises an interrupt (e.g. an `Agent` using human-in-the-loop); deterministic/`MultiAgentBase`
nodes that never interrupt simply complete. `deserialize_state` keys resume off `next_nodes_to_execute`
regardless of which node type paused.

> **What is NOT yet independently confirmed by this spike:** the precise end-to-end handshake for
> resuming a *single interrupt-capable Agent node inside the graph* across processes — i.e. how
> `result.interrupts[0].id` / `[{"interruptResponse": ...}]` threads through `Graph` resume when the
> paused member Agent is reconstructed in a new process. That specific combination is the subject of the
> **separate spike Task 16.1 (Open Question 2)** and should be validated there. This spike (15.1)
> confirms the *graph-level* serialize/deserialize/session contract exists; 16.1 confirms the
> *agent-node interrupt* contract composes with it.

---

## 5. Decision & recommended approach for 15.2 / 15.3

**Decision: keep the custom `execute_incident_graph` thin driver as specified in design §3.2, but
correct the condition signatures and treat native serialize/deserialize as an available fallback/
simplification, not the primary mechanism.**

Rationale:
- The design's core motivation for the custom driver was that native cross-process pause/resume was
  *unverified*. That premise is now **partly resolved** (§4): the graph-level contract is confirmed.
  However, the *agent-node interrupt across processes* piece (Open Question 2) is still pending Spike
  16.1. Until 16.1 confirms it, the custom driver remains the lower-risk path for the demo and keeps
  ThunAI's determinism guarantees (explicit per-node status set, run-progress persistence under the
  incident `Idempotency_Key`, branch continuation on non-terminal failure — Req 4.5–4.9) fully under
  our control rather than depending on SDK internals.
- The custom driver's status vocabulary (`succeeded/failed/timed_out/skipped/not_executed/paused`) and
  run outcomes (`complete/partial/halted/failed/paused`) are richer than what `GraphResult` exposes
  natively, and Req 4.6–4.9 need exactly that granularity. This alone justifies the thin driver.
- Native `serialize_state`/`deserialize_state` + `set_session_manager` should be recorded as the
  **preferred future simplification** (per Open Question 1's own note: "if the SDK's native execution
  *does* support this cleanly, the custom driver could likely be simplified or removed in a later
  iteration"). Re-evaluate after 16.1.

**Concrete guidance for Task 15.2 (`build_incident_graph`):**
- Use `GraphBuilder` exactly as the design lists, with the confirmed method names in §1/§2.
- **Replace** every `condition=lambda ctx: ctx.node_output(X)...` with a
  `Callable[[GraphState], bool]` that reads `state.results[X].result` (§3). Prefer named functions over
  lambdas so the accessor and the `None`-guard are explicit and testable.
- Keep the two-entry-point pattern (`entry="rule_engine"` for sweep, `entry="intake"` for intake) via
  `set_entry_point`; build two graphs from one node/edge factory as designed.
- Wire `set_execution_timeout(600)`, `set_node_timeout(120)`, `set_max_node_executions(25)`.
- Model the `Rule_Engine` node as a `MultiAgentBase` subclass with `invoke_async` (§1).
- Carry `incident_ctx` via `invocation_state`, not via a bespoke `ctx` object.

**Concrete guidance for Task 15.3 (`execute_incident_graph`):**
- Implement the thin driver per design §3.2 (status set, timeouts, run outcomes, branch continuation,
  run-progress persistence on pause) — that spec stands.
- For the pause path, the deterministic-gate pause remains a plain-Python `paused` `NodeResult` (no SDK
  interrupt object), unchanged. For a *Strands `Agent`* node that interrupts, detect
  `AgentResult.stop_reason == "interrupt"` (verify this exact string in 15.3's own verify step; it is
  the design's stated trigger). The graph-level `serialize_state()` output is available as an
  alternative persistence payload if we later choose to lean on native resume.
- Read the timeouts/limits back from the built `Graph`/spec so the driver enforces the same numbers the
  builder was configured with (Req 4.4).

---

## 6. Follow-ups to flag on `design.md`

1. **§3.2 code sample** — change conditional edges from `lambda ctx: ctx.node_output("monitor")...` to
   `def f(state: GraphState) -> bool: ... state.results["monitor"].result ...`. (Open Question 3
   resolved: docs win.)
2. **Open Question 1** — mark "graph-level cross-process pause/resume" as **confirmed** via
   `serialize_state`/`deserialize_state` + `set_session_manager` + `invocation_state` checkpointing;
   downgrade its risk and note the custom driver is retained by choice (determinism + Req 4.6–4.9 status
   granularity), pending Spike 16.1 for the agent-node interrupt composition.
3. **Open Question 3** — mark **resolved**; record the `EdgeConditionWithContext` two-form contract and
   the OR-default / `all_dependencies_complete` AND pattern.
