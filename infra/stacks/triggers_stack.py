"""TriggersStack: the autonomous trigger tier — nothing here is a human typing
(Req 1.1, 1.2, 1.3, 1.4; design.md §3.11).

- ``Sweep_Scheduler``  : EventBridge Scheduler on a fixed cadence (every 10
                         minutes) -> ``triggers/sweep_lambda.py`` -> runtime
                         ``mode=sweep``.
- ``Ingestion_Endpoint``: API Gateway REST endpoint -> ``triggers/ingestion_lambda.py``
                          -> runtime ``mode=intake`` (resident msg / sensor / image).

Both Lambdas call ``invoke_agent_runtime`` on the single AgentCore Runtime,
so each is granted ``bedrock-agentcore:InvokeAgentRuntime`` scoped to that
runtime ARN and given ``THUNAI_RUNTIME_ARN`` in its environment.
"""

from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_scheduler as scheduler
from constructs import Construct

from infra.asset_bundle import lambda_source

_RUNTIME_HANDLER_RUNTIME = lambda_.Runtime.PYTHON_3_13


class TriggersStack(Stack):
    """EventBridge Scheduler + API Gateway + their Lambda targets."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        runtime_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        code = lambda_source()
        common_env = {"THUNAI_RUNTIME_ARN": runtime_arn}

        invoke_runtime_policy = iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[runtime_arn, f"{runtime_arn}/*"],
        )

        # --- sweep Lambda (EventBridge Scheduler target) --------------------
        self.sweep_fn = lambda_.Function(
            self,
            "SweepFn",
            runtime=_RUNTIME_HANDLER_RUNTIME,
            handler="triggers.sweep_lambda.handler",
            code=code,
            timeout=Duration.seconds(60),
            memory_size=256,
            environment=common_env,
        )
        self.sweep_fn.add_to_role_policy(invoke_runtime_policy)

        # EventBridge Scheduler needs a role to invoke the Lambda target.
        scheduler_role = iam.Role(
            self,
            "SweepSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        self.sweep_fn.grant_invoke(scheduler_role)

        self.sweep_schedule = scheduler.CfnSchedule(
            self,
            "SweepSchedule",
            flexible_time_window=scheduler.CfnSchedule.FlexibleTimeWindowProperty(
                mode="OFF"
            ),
            schedule_expression="rate(10 minutes)",
            schedule_expression_timezone="Asia/Kolkata",
            target=scheduler.CfnSchedule.TargetProperty(
                arn=self.sweep_fn.function_arn,
                role_arn=scheduler_role.role_arn,
            ),
        )

        # --- ingestion Lambda (API Gateway target) --------------------------
        self.ingestion_fn = lambda_.Function(
            self,
            "IngestionFn",
            runtime=_RUNTIME_HANDLER_RUNTIME,
            handler="triggers.ingestion_lambda.handler",
            code=code,
            timeout=Duration.seconds(30),
            memory_size=256,
            environment=common_env,
        )
        self.ingestion_fn.add_to_role_policy(invoke_runtime_policy)

        self.api = apigateway.LambdaRestApi(
            self,
            "IngestionApi",
            handler=self.ingestion_fn,
            proxy=False,
            rest_api_name="thunai-ingestion",
            deploy_options=apigateway.StageOptions(stage_name="prod"),
        )
        intake = self.api.root.add_resource("intake")
        intake.add_method("POST")
