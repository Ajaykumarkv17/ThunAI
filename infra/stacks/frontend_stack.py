"""FrontendStack: AWS Amplify Hosting App + Branch for the three React
surfaces (Req 19.1; design.md §3.8, §3.11).

React 18 + TypeScript + Vite, built via ``frontend/amplify.yml``
(``npm ci && npm run build``, artifact ``dist/``). Env vars for the AppSync
Events endpoint and the Cognito pool/app-client are injected as Amplify
build-time environment variables, wired from the Realtime and Auth stack
outputs.

Uses the stable ``aws_amplify.CfnApp``/``CfnBranch`` L1 constructs (the
``aws-amplify-alpha`` L2 package is not a pinned dependency, so the L1 surface
is used to keep the deploy self-contained on the pinned ``aws-cdk-lib``).

The Git source is configured from environment (``THUNAI_FRONTEND_REPO`` etc.);
left unset, the App is created without an attached repository so ``cdk
deploy`` still succeeds and the repository can be connected in the Amplify
console or a follow-up deploy once the OAuth token is available.
"""

from __future__ import annotations

import os

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_amplify as amplify
from constructs import Construct

_REPO_URL = os.environ.get("THUNAI_FRONTEND_REPO", "")
_OAUTH_TOKEN = os.environ.get("THUNAI_FRONTEND_OAUTH_TOKEN", "")
_BRANCH = os.environ.get("THUNAI_FRONTEND_BRANCH", "main")


class FrontendStack(Stack):
    """Amplify Hosting App + Branch, env vars wired from other stacks."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        appsync_http_endpoint: str,
        appsync_realtime_endpoint: str,
        cognito_pool_id: str,
        cognito_app_client_id: str,
        escalation_api_url: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        env_vars = [
            amplify.CfnApp.EnvironmentVariableProperty(name=name, value=value)
            for name, value in {
                "VITE_APPSYNC_HTTP_ENDPOINT": appsync_http_endpoint,
                "VITE_APPSYNC_REALTIME_ENDPOINT": appsync_realtime_endpoint,
                "VITE_COGNITO_USER_POOL_ID": cognito_pool_id,
                "VITE_COGNITO_APP_CLIENT_ID": cognito_app_client_id,
                "VITE_ESCALATION_API_URL": escalation_api_url,
                "VITE_AWS_REGION": self.region,
            }.items()
        ]

        # amplify.yml build spec (Vite build, dist/ artifact).
        build_spec = (
            "version: 1\n"
            "frontend:\n"
            "  phases:\n"
            "    preBuild:\n"
            "      commands:\n"
            "        - cd frontend\n"
            "        - npm ci\n"
            "    build:\n"
            "      commands:\n"
            "        - npm run build\n"
            "  artifacts:\n"
            "    baseDirectory: frontend/dist\n"
            "    files:\n"
            "      - '**/*'\n"
            "  cache:\n"
            "    paths:\n"
            "      - frontend/node_modules/**/*\n"
        )

        app_kwargs: dict = {
            "name": "thunai-frontend",
            "environment_variables": env_vars,
            "build_spec": build_spec,
        }
        if _REPO_URL and _OAUTH_TOKEN:
            app_kwargs["repository"] = _REPO_URL
            app_kwargs["oauth_token"] = _OAUTH_TOKEN

        self.app = amplify.CfnApp(self, "FrontendApp", **app_kwargs)

        self.branch = amplify.CfnBranch(
            self,
            "FrontendBranch",
            app_id=self.app.attr_app_id,
            branch_name=_BRANCH,
            enable_auto_build=bool(_REPO_URL and _OAUTH_TOKEN),
        )

        CfnOutput(self, "AmplifyAppId", value=self.app.attr_app_id)
        CfnOutput(self, "AmplifyDefaultDomain", value=self.app.attr_default_domain)
