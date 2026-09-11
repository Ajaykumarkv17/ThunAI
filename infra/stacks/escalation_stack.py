"""EscalationStack: Escalation_Service Lambda, SNS/SES notification, timeout
sweeper, and a resolution HTTP endpoint (Req 11.2; design.md §3.11, §5.1).

- ``EscalationFn``    : fronts ``surface/escalation_service.py`` for the
                        coordinator's one-tap resolution (create/resolve +
                        ``invoke_agent_runtime`` mode=resume). Exposed via an
                        API Gateway endpoint whose URL the frontend uses.
- ``TimeoutSweeperFn``: ``triggers/timeout_sweeper_lambda.py`` on a 1-minute
                        EventBridge rule — applies the Escalation_Policy
                        default action once a deadline passes (Req 11.6).

Both Lambdas read/write ``thunai-state`` (escalation records) and may deliver
notifications through SNS SMS + SES email; the timeout sweeper and the
resolution handler both invoke the runtime (mode=resume), so both are granted
``bedrock-agentcore:InvokeAgentRuntime`` on the runtime ARN.
"""

from __future__ import annotations

from aws_cdk import Duration, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from constructs import Construct
from infra.asset_bundle import lambda_source

_HANDLER_RUNTIME = lambda_.Runtime.PYTHON_3_13


class EscalationStack(Stack):
    """Escalation_Service Lambda + SNS/SES + 1-minute timeout sweeper."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        state_table,
        runtime_arn: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        code = lambda_source()
        common_env = {
            "THUNAI_RUNTIME_ARN": runtime_arn,
            "THUNAI_STATE_TABLE": state_table.table_name,
        }

        invoke_runtime_policy = iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[runtime_arn, f"{runtime_arn}/*"],
        )
        notify_policy = iam.PolicyStatement(
            actions=["sns:Publish", "ses:SendEmail", "ses:SendRawEmail"],
            resources=["*"],
        )

        # --- escalation resolution Lambda + endpoint ------------------------
        self.escalation_fn = lambda_.Function(
            self,
            "EscalationFn",
            runtime=_HANDLER_RUNTIME,
            handler="surface.escalation_service.handler",
            code=code,
            timeout=Duration.seconds(30),
            memory_size=256,
            environment=common_env,
        )
        state_table.grant_read_write_data(self.escalation_fn)
        self.escalation_fn.add_to_role_policy(invoke_runtime_policy)
        self.escalation_fn.add_to_role_policy(notify_policy)

        self.api = apigateway.LambdaRestApi(
            self,
            "EscalationApi",
            handler=self.escalation_fn,
            proxy=False,
            rest_api_name="thunai-escalation",
            deploy_options=apigateway.StageOptions(stage_name="prod"),
        )
        resolve = self.api.root.add_resource("resolve")
        resolve.add_method("POST")
        self.api_url = self.api.url

        # --- 1-minute timeout sweeper --------------------------------------
        self.timeout_fn = lambda_.Function(
            self,
            "TimeoutSweeperFn",
            runtime=_HANDLER_RUNTIME,
            handler="triggers.timeout_sweeper_lambda.handler",
            code=code,
            timeout=Duration.seconds(60),
            memory_size=256,
            environment=common_env,
        )
        state_table.grant_read_write_data(self.timeout_fn)
        self.timeout_fn.add_to_role_policy(invoke_runtime_policy)
        self.timeout_fn.add_to_role_policy(notify_policy)

        self.timeout_rule = events.Rule(
            self,
            "TimeoutSweeperRule",
            schedule=events.Schedule.rate(Duration.minutes(1)),
        )
        self.timeout_rule.add_target(targets.LambdaFunction(self.timeout_fn))
