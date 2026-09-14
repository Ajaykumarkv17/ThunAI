"""ResponderApiStack: the responder assignment read/respond API (Req 13.1-13.10).

A single Lambda behind API Gateway serving the Responder_Interface:
    GET  /responders/{responderId}/assignments
    POST /responders/{responderId}/assignments/{assignmentId}/respond
Reads/writes assignment items in ``thunai-state``.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_apigateway as apigateway
from aws_cdk import aws_lambda as lambda_
from constructs import Construct

from infra.asset_bundle import lambda_source

_HANDLER_RUNTIME = lambda_.Runtime.PYTHON_3_13


class ResponderApiStack(Stack):
    """Responder assignment read/respond API."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        state_table,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.fn = lambda_.Function(
            self,
            "ResponderApiFn",
            runtime=_HANDLER_RUNTIME,
            handler="surface.responder_api.handler",
            code=lambda_source(),
            timeout=Duration.seconds(15),
            memory_size=256,
            environment={"THUNAI_STATE_TABLE": state_table.table_name},
        )
        state_table.grant_read_write_data(self.fn)

        self.api = apigateway.LambdaRestApi(
            self,
            "ResponderApi",
            handler=self.fn,
            proxy=False,
            rest_api_name="thunai-responder",
            deploy_options=apigateway.StageOptions(stage_name="prod"),
        )
        cors = apigateway.CorsOptions(
            allow_origins=apigateway.Cors.ALL_ORIGINS,
            allow_methods=["GET", "POST", "OPTIONS"],
        )
        responders = self.api.root.add_resource("responders")
        by_id = responders.add_resource("{responderId}")
        assignments = by_id.add_resource("assignments", default_cors_preflight_options=cors)
        assignments.add_method("GET")
        one = assignments.add_resource("{assignmentId}")
        respond = one.add_resource("respond", default_cors_preflight_options=cors)
        respond.add_method("POST")

        # Coordinator active-dispatches view: GET /assignments (all).
        all_assignments = self.api.root.add_resource(
            "assignments", default_cors_preflight_options=cors
        )
        all_assignments.add_method("GET")

        self.api_url = self.api.url
        CfnOutput(self, "ResponderApiUrl", value=self.api.url)
