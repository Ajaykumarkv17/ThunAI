# ThunAI - Quickstart and Deployment Guide

ThunAI is a neighbourhood emergency-response agent that runs autonomously and
only surfaces to a human when a real decision must be made. This guide takes you
from a clean clone to (a) running the credential-free demo loop locally and
(b) deploying the full backend to AWS with the frontend running on your machine.

Single-user note: this guide is written for one developer running a personal
demo/dev deployment. Cost figures in the last section are scoped to that.

---

## 0. What state is the project in?

- Application code is complete: all agents, orchestration, escalation/HITL, the
  AgentCore entrypoint, memory, trigger Lambdas, all 9 CDK stacks, and the three
  React frontends are implemented.
- The credential-free eval/demo loop passes end to end: `python -m evals`
  reports 25/25 scenarios (100%).
- Not yet done: CI workflow, credential-scan script, the full submission README,
  and a validated real-AWS deploy. A first `cdk deploy` has never run against a
  live account, so budget time for first-deploy issues (IAM, Bedrock model
  access, AgentCore arm64 packaging, Knowledge Base ingestion).

---

## 1. Prerequisites

### Local tooling
- Python 3.10+ (repo pins deps for >=3.10)
- Node.js 18+ and npm (for the frontend and the AWS CDK CLI)
- AWS CDK CLI: `npm install -g aws-cdk`
- Docker (AgentCore Runtime packages an arm64 container image; CDK asset
  bundling and the AgentCore build expect a working container runtime)
- Git Bash or WSL to run the .sh helper scripts on Windows

### AWS account setup (do this BEFORE cdk deploy)
1. An AWS account with credentials configured (aws configure / SSO) and
   permissions to create the services in section 4.
2. Region: this guide uses us-west-2 (the .env.example default). Pick one and
   use it consistently.
3. Enable Amazon Bedrock model access for BOTH models in that region:
   us.amazon.nova-lite-v1:0 and us.amazon.nova-pro-v1:0
   (Bedrock Console -> Model access). Without this, every agent model call
   fails at runtime. ThunAI only uses Amazon Nova.
4. Bootstrap CDK once per account/region:
   cdk bootstrap aws://<ACCOUNT_ID>/us-west-2
5. SES/SNS start in sandbox mode in a new account. For a single-user demo,
   verify your own email (SES) and phone (SNS SMS) as coordinator destinations.

---

## 2. Clone, install, configure

    git clone <your-repo-url> ThunAI
    cd ThunAI

    python -m venv .venv
    # Windows PowerShell:  .venv\Scripts\Activate.ps1
    # Git Bash / WSL:      source .venv/bin/activate
    pip install -e .[test]

    cd frontend && npm install && cd ..

    cp .env.example .env      # then fill in the blanks

Key .env values:
- AWS_REGION (e.g. us-west-2)
- THUNAI_LOW_COST_MODEL_ID=us.amazon.nova-lite-v1:0
- THUNAI_HIGH_CAPABILITY_MODEL_ID=us.amazon.nova-pro-v1:0
- SYNTHETIC_SENSORS=1 (leave at 1 - the seeded rising-river demo replays the
  committed hazard fixtures; every OTHER service stays live once deployed)
- Table names default to thunai-state / thunai-audit / thunai-memory.
- Resource-id blanks (THUNAI_KNOWLEDGE_BASE_ID, THUNAI_MEMORY_ID, Cognito ids,
  AppSync endpoints, runtime ARN, etc.) come from CDK stack outputs after deploy.
- Escalation notifications: THUNAI_SES_SENDER, THUNAI_COORDINATOR_EMAIL,
  THUNAI_COORDINATOR_PHONE.

---

## 3. Run the demo loop locally (no AWS, no credentials)

Proves the judged loop - trigger -> autonomous work -> one escalation ->
resume -> audit - against in-process doubles of every external interface.

    ./scripts/demo.sh        # one command (Git Bash / WSL)
    python -m evals          # or directly, cross-platform

Expected tail:

    Scenarios: 25   Passed: 25   Failed: 0
    Goal-success rate: 100.0%

Test suite:

    python -m pytest tests/unit -q         # ~610 unit tests
    python -m pytest tests/integration -q  # core autonomous loop

---

## 4. Deploy the backend to AWS

One Python CDK app (infra/app.py) with nine stacks wired in dependency order,
so a single command provisions everything.

    export CDK_DEFAULT_ACCOUNT=<ACCOUNT_ID>
    export CDK_DEFAULT_REGION=us-west-2     # match AWS_REGION

    cdk synth        # synthesize first - catches errors before touching AWS
    cdk deploy --all # Data -> Auth -> Knowledge -> AgentCore -> Triggers ->
                     # Escalation -> Realtime -> Observability -> Frontend

Deploy order is enforced by explicit add_dependency calls, so --all sequences
correctly. Copy the stack Outputs (table names, Cognito ids, Knowledge Base id,
AgentCore Memory id + runtime ARN, AppSync endpoints) into .env and the frontend
env (section 5).

Seed the deployed account (idempotent):

    ./scripts/seed.sh

First-deploy gotchas:
- Bedrock Nova access not enabled in the region -> model calls fail.
- AgentCore Runtime expects an arm64 container image; infra/checks/arch_guard.py
  fails cdk synth early if the artefact is not arm64 - build on/for arm64.
- Knowledge Base ingestion must finish before knowledge answers work.
- SES/SNS sandbox reaches only verified destinations until production access.

Cost control shipped with the deploy:
- Per-run SpendCapHook backstop: $2.00/run.
- Account-level AWS Budgets alarm: $20 (observability_stack.py).

---

## 5. Run the frontend locally against the deployed backend

    cd frontend
    # Create frontend/.env.local with the stack outputs (Cognito pool/client
    # id, AppSync http+realtime endpoints, escalation API url), then run:
    npm run dev

Vite serves three surfaces on localhost:
- /            public Resident Status Page (no auth)
- /console     Coordinator Console (Cognito group: coordinators)
- /responder   Responder Interface (Cognito group: responders)

Create Cognito users and assign them to the coordinators / responders groups so
you can sign in. You can skip deploying the Amplify Frontend stack while
developing locally.

---

## 6. Tear down

    cdk destroy --all

Do this when done to stop ongoing charges.

---

## 7. Cost analysis - single user, light usage

Design-time estimates, not measured figures. Verify current Bedrock Nova and
AgentCore pricing in the AWS console for your region before relying on exact
numbers.

### Per-run model cost (dominant variable cost)
| Run type | Est. cost/run |
|---|---|
| Routine sweep, no incident change | < $0.01 |
| Full sweep (incident + alert + dispatch + 1 escalation) | ~$0.05 - $0.08 |
| Resume after coordinator approval | ~$0.01 |
| Coordinator chat turn | ~$0.02 |

### Monthly, for one user doing personal dev/demo
| Service | Expected cost |
|---|---|
| Bedrock (Nova Lite + Nova Pro) | A few dollars; under $10 even with active testing |
| DynamoDB, Lambda, API Gateway, SNS/SES, AppSync | Pay-per-use; negligible at this scale |
| Bedrock Knowledge Base (S3 Vectors) | A few dollars one-time embedding + tiny per-query; NO always-on baseline |
| AWS Amplify Hosting | Free-tier friendly; skip if running frontend locally |

Realistic total: under $10/month if you manage the two always-on items below.
The design targets staying inside a $50 AWS credit.

### Two gotchas that can accrue cost while idle
1. AgentCore Runtime may bill for provisioned/idle compute depending on runtime
   mode. Tear it down when not in use.
2. EventBridge Scheduler fires sweeps on a schedule, so the agent runs (and
   spends tokens) even when you are not watching. For single-user testing, slow
   the schedule or disable the rule in the Triggers stack.

### Built-in guardrails
- SpendCapHook per-run cap: $2.00 (runaway-loop backstop, far above ~$0.08/run).
- AWS Budgets alarm at $20, below the $50 credit.

---

## 8. Handy commands

    python -m evals                 # credential-free demo loop (25 scenarios)
    ./scripts/demo.sh               # same loop, one command, time-bounded
    python -m pytest tests -q       # full test suite
    cdk synth                       # validate infra without deploying
    cdk deploy --all                # provision the backend
    ./scripts/seed.sh               # seed the deployed account (idempotent)
    cdk destroy --all               # tear everything down