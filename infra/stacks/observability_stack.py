"""ObservabilityStack: CloudWatch/X-Ray Transaction Search enablement and an
AWS Budgets cost backstop (Req 20.4; design.md §3.11, Cost Model).

- **Transaction Search** — enables X-Ray to index trace spans so agent runs
  are searchable/queryable end to end (one ``run_id`` per invocation, per
  Design Question 1). Enabled via the X-Ray ``CfnTransactionSearchConfig`` L1.
- **Budget backstop (Req 20.4)** — an AWS Budgets monthly cost budget with an
  alarm at the configured $20 backstop threshold, notifying by email at 80%
  (forecasted) and 100% (actual) so a runaway cost is caught well before the
  $50 hackathon credit is exhausted.
"""

from __future__ import annotations

import os

from aws_cdk import Stack
from aws_cdk import aws_budgets as budgets
from aws_cdk import aws_xray as xray
from constructs import Construct

#: Req 20.4 cost backstop, in whole USD.
_BUDGET_LIMIT_USD = float(os.environ.get("THUNAI_BUDGET_LIMIT_USD", "20"))
_BUDGET_EMAIL = os.environ.get("THUNAI_COORDINATOR_EMAIL", "")


class ObservabilityStack(Stack):
    """Transaction Search enablement + AWS Budgets cost alarm."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        agent_runtime_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Transaction Search (X-Ray span indexing) -----------------------
        self.transaction_search = xray.CfnTransactionSearchConfig(
            self,
            "TransactionSearch",
            indexing_percentage=100,
        )

        # --- AWS Budgets $20 backstop (Req 20.4) ----------------------------
        subscribers: list = []
        if _BUDGET_EMAIL:
            subscribers = [
                budgets.CfnBudget.SubscriberProperty(
                    address=_BUDGET_EMAIL, subscription_type="EMAIL"
                )
            ]

        notifications = []
        if subscribers:
            notifications = [
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="FORECASTED",
                        threshold=80,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=subscribers,
                ),
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        comparison_operator="GREATER_THAN",
                        notification_type="ACTUAL",
                        threshold=100,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=subscribers,
                ),
            ]

        self.budget = budgets.CfnBudget(
            self,
            "CostBackstop",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_type="COST",
                time_unit="MONTHLY",
                budget_name=f"thunai-{agent_runtime_name}-backstop",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=_BUDGET_LIMIT_USD, unit="USD"
                ),
            ),
            notifications_with_subscribers=notifications or None,
        )
