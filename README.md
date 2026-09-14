# ThunAI — Neighbourhood Emergency Response Agent

**From early warning to real-world action — powered by Strands Agents and Amazon Bedrock AgentCore.**

ThunAI is a neighbourhood flood-emergency-response agent that runs autonomously and
only surfaces to a human when a real decision must be made. It monitors hazard
sensors, triages resident requests, dispatches responders, drafts multilingual
alerts, and escalates irreversible or low-confidence decisions to a coordinator.

The guiding principle is **appropriate autonomy**, not maximum autonomy:

> **Act** when the situation is clear · **Ask** a human when it is uncertain or
> irreversible · **Stay quiet** when no action is needed.

Every autonomy decision in the system flows through one place — the
`Escalation_Policy` — so the agent's behaviour is predictable and safe to reason
about.

## What it does

`Signal → Understand → Decide → Act → Verify`

- **Detects** — a scheduled sweep (every 10 minutes) reads river/rainfall/dam
  sensors; residents can also send messages in Tamil or English.
- **Assesses** — a deterministic Rule Engine bands the hazard
  (NORMAL / WATCH / WARNING / EVACUATE) and a Monitor opens an incident.
- **Decides** — the incident graph reasons over the situation and routes each
  proposed action through the escalation policy.
- **Acts / Asks** — safe actions (status updates, drafting warnings) proceed
  automatically; consequential ones (mass-audience evacuation advisory,
  irreversible boat dispatch) are held for a coordinator's approval.
- **Coordinates & verifies** — an approved dispatch hands off live to a
  responder, whose progress (en route → on scene → complete) flows back to the
  coordinator and the public, with every step recorded in an append-only audit
  ledger.

## Architecture

Six specialists run as one fixed **incident graph** inside the AgentCore Runtime:

```
rule_engine → monitor → intake → dispatch / alert → safety_qa
```

- **Rule Engine** — deterministic severity banding (no model call).
- **Monitor** — detects abnormal change, raises/updates an incident.
- **Intake** — turns a resident message into a structured request.
- **Dispatch** — matches the best responder / shelter to the need.
- **Alert** — drafts a warning in the local language (Tamil / English).
- **Safety / QA** — reviews high-risk actions before they are carried out.

**AWS building blocks**

| Concern            | Service                                                        |
|--------------------|----------------------------------------------------------------|
| Agent runtime      | Amazon Bedrock AgentCore (Runtime, Memory, Gateway, Observability) |
| Reasoning          | Amazon Nova (`nova-lite`, `nova-pro`)                          |
| Knowledge (RAG)    | Bedrock Knowledge Base on Amazon S3 Vectors                   |
| Application logic  | AWS Lambda (Python 3.13)                                      |
| APIs               | Amazon API Gateway (resident / escalation / responder / ingestion) |
| Identity           | Amazon Cognito (coordinators / responders groups)            |
| State & audit      | Amazon DynamoDB (`thunai-state`, `thunai-audit`, `thunai-memory`) + S3 |
| Scheduling         | Amazon EventBridge (10-min sweep, 1-min timeout)             |
| Notifications      | Amazon SNS (SMS) + Amazon SES (email)                        |
| Realtime           | AWS AppSync Events (DynamoDB Streams → publisher → surfaces) |
| Hosting            | AWS Amplify + CloudFront (React + Vite)                      |
| Observability      | Amazon CloudWatch + AWS X-Ray                                |

See [`docs/thunai_architecture.drawio`](docs/thunai_architecture.drawio) for the
full wiring diagram and [`docs/architecture_ascii.txt`](docs/architecture_ascii.txt)
for a text version.

## The three surfaces

- **Public status page** — unauthenticated; shows the current band, a plain-language
  reason, affected areas, shelters, and current-levels-versus-threshold. Available
  in Tamil and English, with a day/night theme.
- **Coordinator console** — the human-in-the-loop decision inbox, open-incident
  list, active dispatches, and per-incident audit trail.
- **Responder app** — the assignment lifecycle (accept → en route → on scene →
  complete), each stage time-stamped.

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

## Getting started

Copy the environment template and fill in the blanks (`.env` is gitignored and
must never be committed):

```bash
cp .env.example .env
```

The only flag that selects a synthetic backend is `SYNTHETIC_SENSORS` (defaults
to `1`), which replays the committed hazard-reading fixtures so a clean clone
runs the seeded rising-river sweep with no third-party credentials. State,
Knowledge Base, SNS/SES, AppSync, and Cognito remain live.

See [`QUICKSTART.md`](QUICKSTART.md) for deploy and demo steps.

## Demo tooling

Scripts for driving a live demo (run from the `ThunAI` folder with the
environment loaded, e.g. `set -a; . ./.env; set +a`):

- **Change the public status band:**
  ```bash
  .venv/bin/python scripts/set_band.py NORMAL|WATCH|WARNING|EVACUATE
  ```
- **Reset the coordinator/responder demo state** (pushes two fresh escalations
  with the real 30-minute deadline, refreshes the incident, clears responder
  assignments so the live handshake creates one on approval):
  ```bash
  .venv/bin/python scripts/demo_reset.py
  ```
  Set `THUNAI_SEED_ASSIGNMENT=1` to pre-seed a responder assignment instead of
  creating it via the live coordinator→responder handshake.

## Repository layout

```
agents/        the six-node incident graph + specialist agents
policy/        rule engine rules, escalation policy, safety policy
surface/       resident / responder / escalation service handlers
memory/        DynamoDB state store + audit ledger
integrations/  sensor / notification / knowledge providers
triggers/      sweep, ingestion, timeout, stream-publisher Lambdas
infra/         AWS CDK stacks
frontend/      React + Vite (resident / coordinator / responder surfaces)
evals/         recorded scenario suite + harness
scripts/       demo tooling (set_band, demo_reset, seed)
docs/          architecture diagrams
```

## Note

ThunAI is a community coordination aid, not an official emergency service.
Official emergency instructions always take precedence.
