#!/usr/bin/env python3
"""ThunAI CDK application entry point (Req 19.1; design.md §3.11).

One Python CDK ``App`` instantiating nine stacks in dependency order so a
single ``cdk deploy --all`` — with no manual console step — provisions every
cloud resource Req 19.1 lists:

    Data -> Auth -> Knowledge -> AgentCore -> Triggers -> Escalation
         -> Realtime -> Observability -> Frontend

``KnowledgeStack`` has no dependency on Data/Auth, so it deploys in parallel
with them; ``AgentCoreStack`` depends on all three because the runtime needs
the table handles, the Cognito pool, and the Knowledge Base id at deploy time.
Explicit ``add_dependency`` calls are made even where construct-prop references
already force the order, so ``cdk diff`` reads the intended DAG at a glance.

Run from the repository root (so ``lambda_.Code.from_asset(".")`` bundles the
project) with a configured ``CDK_DEFAULT_ACCOUNT``/``CDK_DEFAULT_REGION``.
"""

from __future__ import annotations

import os

from aws_cdk import App, Environment

from infra.stacks import (
    AgentCoreStack,
    AuthStack,
    DataStack,
    EscalationStack,
    KnowledgeStack,
    ObservabilityStack,
    RealtimeStack,
    ResidentStack,
    ResponderApiStack,
    TriggersStack,
)

app = App()

env = Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-west-2")),
)

# 1. Data tier — DynamoDB tables (+ Streams, GSIs) and working S3 bucket.
data = DataStack(app, "ThunaiData", env=env)

# 2. Auth tier — Cognito user pool + coordinator/responder groups.
auth = AuthStack(app, "ThunaiAuth", env=env)

# 3. Knowledge tier — Bedrock Knowledge Base on S3 Vectors (parallel to 1/2).
knowledge = KnowledgeStack(app, "ThunaiKnowledge", env=env)

# 4. AgentCore tier — Runtime, Memory, Gateway, Identity, one exec role.
agentcore = AgentCoreStack(
    app,
    "ThunaiAgentCore",
    state_table=data.state_table,
    audit_table=data.audit_table,
    memory_table=data.memory_table,
    working_bucket=data.working_bucket,
    cognito_pool=auth.user_pool,
    knowledge_base_id=knowledge.knowledge_base_id,
    env=env,
)

# 5. Triggers tier — EventBridge Scheduler, API Gateway, trigger Lambdas.
triggers = TriggersStack(
    app,
    "ThunaiTriggers",
    runtime_arn=agentcore.runtime.attr_agent_runtime_arn,
    env=env,
)

# 6. Escalation tier — Escalation_Service Lambda, SNS/SES, timeout sweeper.
escalation = EscalationStack(
    app,
    "ThunaiEscalation",
    state_table=data.state_table,
    audit_table=data.audit_table,
    runtime_arn=agentcore.runtime.attr_agent_runtime_arn,
    env=env,
)

# 7. Realtime tier — AppSync Events + channel namespaces + stream publisher.
realtime = RealtimeStack(
    app,
    "ThunaiRealtime",
    state_table=data.state_table,
    cognito_pool=auth.user_pool,
    env=env,
)

# 7b. Resident tier — public read API for the Resident_Status_Page.
resident = ResidentStack(
    app,
    "ThunaiResident",
    state_table=data.state_table,
    env=env,
)
resident.add_dependency(data)

# 7c. Responder tier — assignment read/respond API for the Responder_Interface.
responder_api = ResponderApiStack(
    app,
    "ThunaiResponderApi",
    state_table=data.state_table,
    env=env,
)
responder_api.add_dependency(data)

# 8. Observability tier — Transaction Search + AWS Budgets backstop.
observability = ObservabilityStack(
    app,
    "ThunaiObservability",
    agent_runtime_name=agentcore.runtime_name,
    env=env,
)

# 9. Frontend tier — Amplify Hosting App + Branch, env vars wired in.
#
# DISABLED for local frontend development: the FrontendStack is intentionally
# not instantiated so `cdk deploy --all` provisions NO Amplify hosting. Run the
# frontend locally instead (`cd frontend && npm run dev`) against the other
# stacks' outputs. To re-enable Amplify hosting later, restore `FrontendStack`
# to the `infra.stacks` import above, then un-comment this block and the three
# `frontend.add_dependency(...)` edges below.
#
# frontend = FrontendStack(
#     app,
#     "ThunaiFrontend",
#     appsync_http_endpoint=realtime.api.http_dns,
#     appsync_realtime_endpoint=realtime.api.realtime_dns,
#     cognito_pool_id=auth.user_pool.user_pool_id,
#     cognito_app_client_id=auth.app_client.user_pool_client_id,
#     escalation_api_url=escalation.api_url,
#     env=env,
# )

# Explicit dependency edges (beyond what construct-prop refs already force),
# so `cdk diff` reads the intended deploy DAG at a glance.
agentcore.add_dependency(data)
agentcore.add_dependency(auth)
agentcore.add_dependency(knowledge)
triggers.add_dependency(agentcore)
escalation.add_dependency(agentcore)
realtime.add_dependency(data)
realtime.add_dependency(auth)
observability.add_dependency(agentcore)
# frontend.add_dependency(realtime)
# frontend.add_dependency(auth)
# frontend.add_dependency(escalation)

app.synth()
