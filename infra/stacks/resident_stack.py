"""ResidentStack: the public Resident_Status_Page read API (Req 14.1-14.9).

A single Lambda behind API Gateway serving ``GET /public/status`` — the
aggregate, unauthenticated public snapshot (severity band, per-area request
counts, shelter capacity, published rule set). Read-only against
``thunai-state``; no auth (the resident page is public per Req 12.5 / §3.8).
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from infra.asset_bundle import lambda_source

_HANDLER_RUNTIME = lambda_.Runtime.PYTHON_3_13


class ResidentStack(Stack):
    """Public read API for the Resident_Status_Page."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        state_table,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.status_fn = lambda_.Function(
            self,
            "ResidentStatusFn",
            runtime=_HANDLER_RUNTIME,
            handler="surface.resident_api.handler",
            code=lambda_source(),
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={
                "THUNAI_STATE_TABLE": state_table.table_name,
                # Demo deployment: present seeded readings as current so the
                # public page never shows a stale demo incident mid-presentation.
                "THUNAI_DEMO_MODE": "1",
            },
        )
        state_table.grant_read_data(self.status_fn)

        self.api = apigateway.LambdaRestApi(
            self,
            "ResidentApi",
            handler=self.status_fn,
            proxy=False,
            rest_api_name="thunai-resident",
            deploy_options=apigateway.StageOptions(stage_name="prod"),
        )
        public = self.api.root.add_resource(
            "public",
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=apigateway.Cors.ALL_ORIGINS,
                allow_methods=["GET", "OPTIONS"],
            ),
        )
        status = public.add_resource("status")
        status.add_method("GET")
        self.api_url = self.api.url

        CfnOutput(self, "ResidentApiUrl", value=self.api.url)
