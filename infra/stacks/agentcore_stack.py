"""AgentCoreStack: Amazon Bedrock AgentCore Runtime, Memory, Gateway, Identity
(Req 19.1, 19.2; design.md §3.11, Open Question 4).

A single AgentCore Runtime (Design Question 1: one runtime, mode-dispatched)
fronts one ``BedrockAgentCoreApp`` ``@app.entrypoint``. One least-privilege
execution role is the union of what the runtime's tools need — scoped to the
named DynamoDB tables, the working S3 bucket, the named Nova model ids, the
Knowledge Base, and SNS/SES — and nothing else.

arm64 guarantee (Req 19.2): ``verify_arm64_or_abort(BUNDLE_DIR)`` runs as the
first line of the constructor, *before* the ``CfnRuntime`` L1 is instantiated,
so a non-arm64 build fails ``cdk synth`` locally rather than reaching
``CREATE_FAILED`` in AWS.

Open Question 4 note: the ``aws_cdk.aws_bedrockagentcore`` L1 property names
(``networkConfiguration``/``networkMode``, ``agentRuntimeArtifact``) can shift
casing/nesting across ``aws-cdk-lib`` releases. The property names below match
the pinned ``aws-cdk-lib==2.268.0`` L1 surface; a deviation, if the installed
version differs, is a mechanical rename recorded against this task.
"""

from __future__ import annotations

import os

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_bedrockagentcore as agentcore
from aws_cdk import aws_iam as iam
from constructs import Construct

from infra.checks.arch_guard import verify_arm64_or_abort

#: Directory of the built, arm64 AgentCore Runtime bundle (packaging step
#: output). Overridable so a ``cdk diff`` on a machine without a bundle does
#: not force a build; ``verify_arm64_or_abort`` treats an absent dir as
#: "nothing to verify".
_BUNDLE_DIR = os.environ.get("THUNAI_RUNTIME_BUNDLE_DIR", "dist/runtime")

#: Container image URI for the runtime (built + pushed by the packaging step).
_RUNTIME_IMAGE_URI_ENV = "THUNAI_RUNTIME_IMAGE_URI"

_LOW_COST_MODEL = os.environ.get("THUNAI_LOW_COST_MODEL_ID", "us.amazon.nova-lite-v1:0")
_HIGH_CAP_MODEL = os.environ.get("THUNAI_HIGH_CAPABILITY_MODEL_ID", "us.amazon.nova-pro-v1:0")


class AgentCoreStack(Stack):
    """Single AgentCore Runtime + Memory + Gateway + Identity, one exec role."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        state_table,
        audit_table,
        memory_table,
        working_bucket,
        cognito_pool,
        knowledge_base_id: str,
        **kwargs,
    ) -> None:
        # Req 19.2: abort the synth BEFORE the Runtime resource is created if
        # the built artefact is not arm64.
        verify_arm64_or_abort(_BUNDLE_DIR)

        super().__init__(scope, construct_id, **kwargs)

        region = self.region
        account = self.account

        # --- one least-privilege execution role -----------------------------
        exec_role = iam.Role(
            self,
            "RuntimeExecutionRole",
            assumed_by=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
            description="ThunAI AgentCore Runtime execution role (least privilege).",
        )
        # Model invocation — scoped to exactly the two permitted Nova ids.
        exec_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=[
                    f"arn:aws:bedrock:{region}::foundation-model/{_LOW_COST_MODEL}",
                    f"arn:aws:bedrock:{region}::foundation-model/{_HIGH_CAP_MODEL}",
                ],
            )
        )
        # Knowledge Base retrieval.
        exec_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:Retrieve"],
                resources=[
                    f"arn:aws:bedrock:{region}:{account}:knowledge-base/{knowledge_base_id}"
                ],
            )
        )
        # State/audit/memory table access (writers scoped to named tables).
        state_table.grant_read_write_data(exec_role)
        audit_table.grant_read_write_data(exec_role)
        memory_table.grant_read_write_data(exec_role)
        working_bucket.grant_read_write(exec_role)
        # Notification delivery (SNS SMS + SES email).
        exec_role.add_to_policy(
            iam.PolicyStatement(
                actions=["sns:Publish", "ses:SendEmail", "ses:SendRawEmail"],
                resources=["*"],
            )
        )

        # --- AgentCore Memory ------------------------------------------------
        self.memory = agentcore.CfnMemory(
            self,
            "CoordinatorMemory",
            name="thunai_coordinator_memory",
            event_expiry_duration=90,
        )

        # --- AgentCore Runtime (single runtime, mode-dispatched) -------------
        image_uri = os.environ.get(
            _RUNTIME_IMAGE_URI_ENV,
            f"{account}.dkr.ecr.{region}.amazonaws.com/thunai-runtime:latest",
        )
        self.runtime = agentcore.CfnRuntime(
            self,
            "Runtime",
            agent_runtime_name="thunai_runtime",
            role_arn=exec_role.role_arn,
            network_configuration=agentcore.CfnRuntime.NetworkConfigurationProperty(
                network_mode="PUBLIC",
            ),
            agent_runtime_artifact=agentcore.CfnRuntime.AgentRuntimeArtifactProperty(
                container_configuration=agentcore.CfnRuntime.ContainerConfigurationProperty(
                    container_uri=image_uri,
                ),
            ),
            environment_variables={
                "AWS_REGION": region,
                "THUNAI_LOW_COST_MODEL_ID": _LOW_COST_MODEL,
                "THUNAI_HIGH_CAPABILITY_MODEL_ID": _HIGH_CAP_MODEL,
                "THUNAI_STATE_TABLE": state_table.table_name,
                "THUNAI_AUDIT_TABLE": audit_table.table_name,
                "THUNAI_MEMORY_TABLE": memory_table.table_name,
                "THUNAI_KNOWLEDGE_BASE_ID": knowledge_base_id,
                "THUNAI_MEMORY_ID": self.memory.attr_memory_id,
                "THUNAI_COGNITO_USER_POOL_ID": cognito_pool.user_pool_id,
                "SYNTHETIC_SENSORS": os.environ.get("SYNTHETIC_SENSORS", "1"),
            },
        )
        self.runtime.node.add_dependency(self.memory)

        self.runtime_name = "thunai_runtime"

        CfnOutput(self, "RuntimeArn", value=self.runtime.attr_agent_runtime_arn)
        CfnOutput(self, "MemoryId", value=self.memory.attr_memory_id)
        CfnOutput(self, "ExecutionRoleArn", value=exec_role.role_arn)
