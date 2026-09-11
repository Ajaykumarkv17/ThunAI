"""RealtimeStack: AppSync Events API + channel namespaces + DynamoDB Streams
publisher (Req 12.5; design.md §3.8, §3.11, Open Question 7).

DynamoDB Streams on ``thunai-state`` feed ``triggers/stream_publisher_lambda.py``,
which publishes each committed change to an AppSync Events channel namespaced
by entity type. This turns a DynamoDB commit into the realtime surface update
budgets: ≤5s Coordinator_Console (Req 12.5), ≤10s Decision_Inbox (Req 11.2),
≤15s Resident_Status_Page (Req 14.3).

Open Question 7 resolution — **single Event API, per-namespace auth**. AppSync
Events supports per-``ChannelNamespace`` authorization overrides, so one Event
API is enough:

- ``status`` / ``shelters``     : public read (API key) — the unauthenticated
                                  Resident_Status_Page (Req 14.1).
- ``incidents`` / ``escalations``: Cognito user-pool auth — Coordinator_Console
                                  and Decision_Inbox.

The publisher Lambda authorizes to the API with IAM, so it can publish to
every namespace regardless of the subscribe-side auth mode.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_appsync as appsync
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as event_sources
from constructs import Construct
from infra.asset_bundle import lambda_source

_HANDLER_RUNTIME = lambda_.Runtime.PYTHON_3_13

_API_KEY = appsync.AppSyncAuthorizationType.API_KEY
_USER_POOL = appsync.AppSyncAuthorizationType.USER_POOL
_IAM = appsync.AppSyncAuthorizationType.IAM


class RealtimeStack(Stack):
    """AppSync Events API + channel namespaces + stream publisher Lambda."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        state_table,
        cognito_pool,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        api_key_provider = appsync.AppSyncAuthProvider(authorization_type=_API_KEY)
        cognito_provider = appsync.AppSyncAuthProvider(
            authorization_type=_USER_POOL,
            cognito_config=appsync.AppSyncCognitoConfig(user_pool=cognito_pool),
        )
        iam_provider = appsync.AppSyncAuthProvider(authorization_type=_IAM)

        self.api = appsync.EventApi(
            self,
            "EventApi",
            api_name="thunai-events",
            authorization_config=appsync.EventApiAuthConfig(
                auth_providers=[api_key_provider, cognito_provider, iam_provider],
                connection_auth_mode_types=[_API_KEY, _USER_POOL, _IAM],
                default_publish_auth_mode_types=[_IAM],
                default_subscribe_auth_mode_types=[_API_KEY, _USER_POOL],
            ),
        )

        # Public read namespaces (subscribe: API key; publish: IAM).
        for ns in ("status", "shelters"):
            self.api.add_channel_namespace(
                ns,
                channel_namespace_name=ns,
                authorization_config=appsync.NamespaceAuthConfig(
                    subscribe_auth_mode_types=[_API_KEY],
                    publish_auth_mode_types=[_IAM],
                ),
            )

        # Cognito-gated namespaces (subscribe: user pool; publish: IAM).
        for ns in ("incidents", "escalations"):
            self.api.add_channel_namespace(
                ns,
                channel_namespace_name=ns,
                authorization_config=appsync.NamespaceAuthConfig(
                    subscribe_auth_mode_types=[_USER_POOL],
                    publish_auth_mode_types=[_IAM],
                ),
            )

        # --- DynamoDB Streams -> publisher Lambda ---------------------------
        self.publisher_fn = lambda_.Function(
            self,
            "StreamPublisherFn",
            runtime=_HANDLER_RUNTIME,
            handler="triggers.stream_publisher_lambda.handler",
            code=lambda_source(),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "THUNAI_EVENT_API_HTTP_ENDPOINT": self.api.http_dns,
                "THUNAI_EVENT_API_ID": self.api.api_id,
            },
        )
        self.publisher_fn.add_event_source(
            event_sources.DynamoEventSource(
                state_table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=10,
                retry_attempts=3,
            )
        )
        self.publisher_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["appsync:EventPublish"],
                resources=[self.api.api_arn, f"{self.api.api_arn}/*"],
            )
        )

        CfnOutput(self, "EventApiId", value=self.api.api_id)
        CfnOutput(self, "EventApiHttpEndpoint", value=self.api.http_dns)
        CfnOutput(self, "EventApiRealtimeEndpoint", value=self.api.realtime_dns)
