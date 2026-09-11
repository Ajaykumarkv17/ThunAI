# ThunAI — Neighbourhood Emergency Response Agent

ThunAI is a neighbourhood emergency-response agent that runs autonomously and
only surfaces to a human when a real decision must be made. It monitors hazard
sensors, triages resident requests, dispatches responders, drafts multilingual
alerts, and escalates irreversible or low-confidence decisions to a coordinator.

## Evaluation

<!-- eval-suite-stat:start -->
**Goal-success rate: 100.0%** — 25 / 25 recorded scenarios passing
(`Eval_Suite` version `eval-suite-v1`).
<!-- eval-suite-stat:end -->

The goal-success rate is computed as `passed / total * 100`, rounded to one
decimal place (Req 21.2), over the recorded scenario suite in
[`evals/scenarios/`](evals/scenarios/). Every scenario is replayed against the
real ThunAI decision logic — the deterministic `Rule_Engine` severity banding
and the single `Escalation_Policy` autonomy authority — through in-process
doubles of every external interface, so a run needs **zero** third-party
credentials and makes **no** live AWS call.

Scenario coverage (Req 21.1) includes one scenario per severity band
(NORMAL / WATCH / WARNING / EVACUATE), one intake scenario per configured
language (Tamil, English, and code-mixed Tamil-English), dispatch with and
without responder capacity, escalation approve / decline / timeout, and the
single-escalation-per-sweep demo (Req 11.9) that asserts exactly one
`Escalation_Record` is produced over the seeded rising-river sweep.

### Running the suite

```bash
python -m evals
```

This replays every scenario, writes the per-scenario results and aggregate
goal-success figure to `evals/results/latest.json`, prints the goal-success
rate, and exits non-zero if any scenario fails. Re-run it after changing any
decision logic and update the stat block above from the printed figure.
