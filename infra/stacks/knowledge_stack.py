"""KnowledgeStack: Amazon Bedrock Knowledge Base on Amazon S3 Vectors
(Req 8; design.md sec 3.11, resolves Open Question 6).

The vector store is Amazon S3 Vectors (no always-on baseline cost, unlike
OpenSearch Serverless) -- the deliberate choice for a small curated document
set against the hackathon credit budget. This stack provisions:

- a document-source S3 bucket seeded from ``seed/knowledge_docs/`` by
  ``scripts/seed.sh`` (which then starts the ingestion job);
- an S3 Vectors vector bucket + vector index;
- a Bedrock Knowledge Base with an S3 Vectors storage configuration and a
  Titan/Nova-family embedding model;
- a data source pointing at the document bucket.

``Knowledge_Provider`` queries the KB through the live ``bedrock-agent-runtime``
``Retrieve`` API (the deprecated ``strands-agents-tools`` ``retrieve`` tool is
not used). ``knowledge_base_id`` is exposed as a stack output and injected into
the AgentCore runtime environment as ``THUNAI_KNOWLEDGE_BASE_ID``.

S3 Vectors + the Bedrock Knowledge Base S3-Vectors storage type are exposed
only through L1 (``Cfn*``) constructs at the pinned ``aws-cdk-lib`` version,
so this stack uses L1 resources with an explicit KB service role.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3vectors as s3vectors
from constructs import Construct

#: Amazon Titan Text Embeddings V2 -- a low-cost embedding model suitable for a
#: small curated corpus; output dimension 1024.
_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
_EMBEDDING_DIMENSION = 1024


class KnowledgeStack(Stack):
    """Bedrock Knowledge Base backed by an S3 Vectors vector store."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- document-source bucket (ingested into the KB) ------------------
        self.docs_bucket = s3.Bucket(
            self,
            "KnowledgeDocsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # --- S3 Vectors vector bucket + index -------------------------------
        self.vector_bucket = s3vectors.CfnVectorBucket(
            self,
            "VectorBucket",
            vector_bucket_name=f"thunai-kb-vectors-{self.account}-{self.region}",
        )
        self.vector_index = s3vectors.CfnIndex(
            self,
            "VectorIndex",
            vector_bucket_name=self.vector_bucket.vector_bucket_name,
            index_name="thunai-kb-index",
            data_type="float32",
            dimension=_EMBEDDING_DIMENSION,
            distance_metric="cosine",
        )
        self.vector_index.add_dependency(self.vector_bucket)

        # --- Knowledge Base service role ------------------------------------
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"),
            description="Bedrock Knowledge Base service role for ThunAI (S3 Vectors).",
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{self.region}::foundation-model/{_EMBEDDING_MODEL_ID}"
                ],
            )
        )
        self.docs_bucket.grant_read(kb_role)
        kb_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "s3vectors:GetIndex",
                    "s3vectors:QueryVectors",
                    "s3vectors:PutVectors",
                    "s3vectors:GetVectors",
                    "s3vectors:ListVectors",
                    "s3vectors:DeleteVectors",
                ],
                # Scope to the vector bucket AND its indexes. S3 Vectors
                # authorizes index-level actions (QueryVectors/GetVectors/...)
                # against the index sub-resource ARN, so both are needed.
                resources=[
                    self.vector_bucket.attr_vector_bucket_arn,
                    f"{self.vector_bucket.attr_vector_bucket_arn}/index/*",
                ],
            )
        )

        # --- Knowledge Base + data source (L1) ------------------------------
        self.knowledge_base = bedrock.CfnKnowledgeBase(
            self,
            "KnowledgeBase",
            name="thunai-community-safety",
            role_arn=kb_role.role_arn,
            knowledge_base_configuration=bedrock.CfnKnowledgeBase.KnowledgeBaseConfigurationProperty(
                type="VECTOR",
                vector_knowledge_base_configuration=bedrock.CfnKnowledgeBase.VectorKnowledgeBaseConfigurationProperty(
                    embedding_model_arn=f"arn:aws:bedrock:{self.region}::foundation-model/{_EMBEDDING_MODEL_ID}",
                ),
            ),
            storage_configuration=bedrock.CfnKnowledgeBase.StorageConfigurationProperty(
                type="S3_VECTORS",
                s3_vectors_configuration=bedrock.CfnKnowledgeBase.S3VectorsConfigurationProperty(
                    index_arn=self.vector_index.attr_index_arn,
                ),
            ),
        )
        self.knowledge_base.node.add_dependency(self.vector_index)
        # CRITICAL: Bedrock synchronously calls s3vectors:QueryVectors while
        # validating the storage config at create time. The role's inline
        # permissions live in a separate `KnowledgeBaseRoleDefaultPolicy`
        # resource that `role_arn` alone does NOT order against, so without
        # this the KB can be created before the policy attaches and fail 403.
        # Depend on the whole role node so every attached policy exists first.
        self.knowledge_base.node.add_dependency(kb_role)
        if kb_role.node.try_find_child("DefaultPolicy") is not None:
            self.knowledge_base.node.add_dependency(
                kb_role.node.find_child("DefaultPolicy")
            )

        self.data_source = bedrock.CfnDataSource(
            self,
            "KnowledgeDataSource",
            knowledge_base_id=self.knowledge_base.attr_knowledge_base_id,
            name="thunai-docs-source",
            data_source_configuration=bedrock.CfnDataSource.DataSourceConfigurationProperty(
                type="S3",
                s3_configuration=bedrock.CfnDataSource.S3DataSourceConfigurationProperty(
                    bucket_arn=self.docs_bucket.bucket_arn,
                ),
            ),
        )

        self.knowledge_base_id = self.knowledge_base.attr_knowledge_base_id

        CfnOutput(self, "KnowledgeBaseId", value=self.knowledge_base_id)
        CfnOutput(self, "KnowledgeDataSourceId", value=self.data_source.attr_data_source_id)
        CfnOutput(self, "KnowledgeDocsBucketName", value=self.docs_bucket.bucket_name)
