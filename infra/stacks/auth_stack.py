"""AuthStack: Amazon Cognito user pool + coordinator/responder groups
(Req 12.9, 13.6; design.md §3.8, §3.11).

Provisions the identity provider the three React surfaces authenticate
against. Two Cognito groups back the role separation the requirements draw:

- ``coordinators`` : the ward coordinator role — Decision_Inbox, incident
                     list, audit view, ad-hoc chat (Req 12.9).
- ``responders``   : registered responders — accept/progress/complete an
                     assignment (Req 13.6).

The public Resident_Status_Page is unauthenticated (Req 12.5 / §3.8) and does
not use this pool; only the coordinator and responder surfaces do.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_cognito as cognito
from constructs import Construct


class AuthStack(Stack):
    """Cognito user pool with coordinator and responder groups."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.user_pool = cognito.UserPool(
            self,
            "UserPool",
            user_pool_name="thunai-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.app_client = self.user_pool.add_client(
            "WebClient",
            user_pool_client_name="thunai-web",
            auth_flows=cognito.AuthFlow(user_srp=True),
            generate_secret=False,
            prevent_user_existence_errors=True,
        )

        self.coordinator_group = cognito.CfnUserPoolGroup(
            self,
            "CoordinatorsGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="coordinators",
            description="Ward coordinators: Decision_Inbox, incidents, audit, chat.",
        )
        self.responder_group = cognito.CfnUserPoolGroup(
            self,
            "RespondersGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="responders",
            description="Registered responders: accept/progress/complete assignments.",
        )

        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(
            self,
            "AppClientId",
            value=self.app_client.user_pool_client_id,
        )
