"""DataStack: DynamoDB tables (+ Streams, GSIs) and S3 buckets (Req 19.1,
15.1, 15.6; design.md sec 3.11, sec 4.4).

Provisions the three DynamoDB tables the persistence layer expects, matching
the exact key schemas the ``memory/`` modules read/write:

- ``thunai-state``  : composite ``(pk, sk)`` primary key, one overloaded GSI
                      ``GSI1`` (``gsi1pk``/``gsi1sk``), DynamoDB Streams enabled
                      (NEW_AND_OLD_IMAGES) to feed the realtime stream publisher.
- ``thunai-audit``  : composite ``(pk, sk)`` primary key, one GSI ``GSI1``
                      for the per-incident oldest-to-newest audit read (Req 12.7).
                      Append-only is enforced in application code
                      (``attribute_not_exists`` conditional PutItem).
- ``thunai-memory`` : composite ``(pk, sk)`` primary key -- 30-day hazard
                      baselines + coordinator preferences.

TTL is enabled on ``thunai-state`` (attribute ``expires_at``) purely as
storage-cost hygiene for the 72h idempotency/dedup rows; the application
never relies on TTL deletion for correctness (see ``memory/state_store.py``).

S3 buckets: a context-offload + hazard-image working bucket. The Bedrock
Knowledge Base document-source bucket and the S3 Vectors vector bucket live
in ``KnowledgeStack``, not here.
"""

from __future__ import annotations

from aws_cdk import RemovalPolicy, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_s3 as s3
from constructs import Construct


class DataStack(Stack):
    """The persisted-state tier: DynamoDB tables and working S3 buckets."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- thunai-state ---------------------------------------------------
        self.state_table = dynamodb.Table(
            self,
            "StateTable",
            table_name="thunai-state",
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            time_to_live_attribute="expires_at",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.state_table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(
                name="gsi1pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="gsi1sk", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # --- thunai-audit (append-only in application code) -----------------
        self.audit_table = dynamodb.Table(
            self,
            "AuditTable",
            table_name="thunai-audit",
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.audit_table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(
                name="gsi1pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="gsi1sk", type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # --- thunai-memory --------------------------------------------------
        self.memory_table = dynamodb.Table(
            self,
            "MemoryTable",
            table_name="thunai-memory",
            partition_key=dynamodb.Attribute(
                name="pk", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="sk", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- S3: runtime working storage (context offload + hazard images) --
        self.working_bucket = s3.Bucket(
            self,
            "WorkingBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=False,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
