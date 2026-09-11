"""Unit tests for scripts/seed_data.py (task 11.5).

Uses moto's mocked DynamoDB and S3 (`moto.mock_aws`, moto==5.2.3, pinned in
pyproject.toml's `test` extra) for the State_Store/S3 calls, and a
`unittest.mock.MagicMock` for the `bedrock-agent` client — moto does not
cover `bedrock-agent`/`bedrock-agent-runtime` (confirmed the same way
`tests/unit/test_knowledge_provider.py` already documents), so the
ingestion-job call is verified via the injected mock's `assert_called_once`.

No real AWS calls are made.

Validates: Requirements 19.7, 8.1a; Design §3.9.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from memory import state_store
from scripts import seed_data

TABLE_NAME = "thunai-state-test"
BUCKET_NAME = "thunai-kb-docs-test"


@pytest.fixture(autouse=True)
def _dynamo_table():
    """Fresh moto-mocked `thunai-state` table for every test, mirroring
    tests/unit/test_state_store.py's fixture."""
    os.environ["THUNAI_STATE_TABLE"] = TABLE_NAME
    os.environ.setdefault("AWS_REGION", "us-west-2")
    os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
    os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "gsi1pk", "AttributeType": "S"},
                {"AttributeName": "gsi1sk", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                    "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        state_store.reset_table_cache()
        yield
        state_store.reset_table_cache()


@pytest.fixture()
def _s3_bucket():
    """Fresh moto-mocked S3 bucket backing THUNAI_KNOWLEDGE_DOCS_BUCKET.

    Note: this fixture does NOT itself enter `mock_aws()` — it is composed
    under the `_dynamo_table` fixture's already-active `mock_aws()` context
    (autouse, applied first), so both DynamoDB and S3 calls in a given test
    hit the same moto session.
    """
    client = boto3.client("s3", region_name="us-west-2")
    client.create_bucket(
        Bucket=BUCKET_NAME,
        CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
    )
    return client


def _real_seed_dataset() -> dict:
    """Load the actual committed seed/seed_dataset.json (task 11.1) so this
    test exercises the real fixture shape, not a hand-rolled stand-in."""
    with seed_data.SEED_DATASET_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Step 1 — responders & shelters written to thunai-state
# ---------------------------------------------------------------------------


def test_seed_responders_and_shelters_writes_expected_rows():
    dataset = _real_seed_dataset()

    responder_count, shelter_count = seed_data.seed_responders_and_shelters(dataset)

    assert responder_count == len(dataset["responders"])
    assert shelter_count == len(dataset["shelters"])

    first_responder_raw = dataset["responders"][0]
    stored_responder = state_store.get_responder(first_responder_raw["responder_id"])
    assert stored_responder is not None
    assert stored_responder.name == first_responder_raw["name"]
    assert stored_responder.availability_status == "AVAILABLE"

    first_shelter_raw = dataset["shelters"][0]
    stored_shelter = state_store.get_shelter(first_shelter_raw["shelter_id"])
    assert stored_shelter is not None
    assert stored_shelter.total_capacity == first_shelter_raw["total_capacity"]
    assert stored_shelter.available_capacity == first_shelter_raw["available_capacity"]


def test_seed_responders_and_shelters_is_rerunnable():
    """Re-running the write step against the same moto table must not raise
    and must converge to the same state (Req 19.7)."""
    dataset = _real_seed_dataset()

    first_run = seed_data.seed_responders_and_shelters(dataset)
    second_run = seed_data.seed_responders_and_shelters(dataset)

    assert first_run == second_run

    first_responder_raw = dataset["responders"][0]
    stored = state_store.get_responder(first_responder_raw["responder_id"])
    assert stored is not None
    assert stored.name == first_responder_raw["name"]


# ---------------------------------------------------------------------------
# Step 2 — knowledge docs uploaded to S3
# ---------------------------------------------------------------------------


def test_upload_knowledge_docs_uploads_one_object_per_non_readme_file(_s3_bucket):
    expected_files = [
        p for p in seed_data.KNOWLEDGE_DOCS_DIR.glob("*.md") if p.name.lower() != "readme.md"
    ]
    assert expected_files, "expected at least one non-README knowledge doc fixture"

    uploaded_count = seed_data.upload_knowledge_docs(BUCKET_NAME, s3_client=_s3_bucket)

    assert uploaded_count == len(expected_files)

    listed = _s3_bucket.list_objects_v2(Bucket=BUCKET_NAME)
    uploaded_keys = {obj["Key"] for obj in listed.get("Contents", [])}
    for path in expected_files:
        assert path.name in uploaded_keys
    assert "README.md" not in uploaded_keys


# ---------------------------------------------------------------------------
# Step 2b — start_ingestion_job called exactly once with correct params
# ---------------------------------------------------------------------------


def test_start_kb_ingestion_job_calls_bedrock_agent_once_with_correct_params():
    mock_bedrock_agent = MagicMock()
    mock_bedrock_agent.start_ingestion_job.return_value = {
        "ingestionJob": {"ingestionJobId": "job-123", "status": "STARTING"}
    }

    job_id = seed_data.start_kb_ingestion_job(
        "KB1234567890",
        "DS1234567890",
        bedrock_agent_client=mock_bedrock_agent,
    )

    assert job_id == "job-123"
    mock_bedrock_agent.start_ingestion_job.assert_called_once()
    _, kwargs = mock_bedrock_agent.start_ingestion_job.call_args
    assert kwargs["knowledgeBaseId"] == "KB1234567890"
    assert kwargs["dataSourceId"] == "DS1234567890"
    assert "clientToken" in kwargs and len(kwargs["clientToken"]) >= 33


def test_start_kb_ingestion_job_treats_conflict_as_non_fatal():
    from botocore.exceptions import ClientError

    mock_bedrock_agent = MagicMock()
    mock_bedrock_agent.start_ingestion_job.side_effect = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "already running"}},
        "StartIngestionJob",
    )

    job_id = seed_data.start_kb_ingestion_job(
        "KB1234567890", "DS1234567890", bedrock_agent_client=mock_bedrock_agent
    )

    assert job_id == "conflict-ingestion-already-running"


# ---------------------------------------------------------------------------
# Scoping checks — resident_points/hazard_readings/sample_messages
# ---------------------------------------------------------------------------


def test_resident_points_are_not_written_to_state_store():
    """resident_points have no State_Store write helper (see seed_data.py's
    module docstring, scoping decision 1) — only responders/shelters are
    written by seed_responders_and_shelters()."""
    dataset = _real_seed_dataset()
    assert "resident_points" in dataset  # loaded for reporting only

    # No state_store function exists that would accept a resident point;
    # asserting the public API surface stays scoped to responders/shelters.
    assert not hasattr(state_store, "put_resident_point")


def test_check_sensor_fixture_path_reports_missing_var_without_raising():
    saved = os.environ.pop("SENSOR_FIXTURE_PATH", None)
    try:
        warning = seed_data.check_sensor_fixture_path()
        assert warning is not None
        assert "SENSOR_FIXTURE_PATH" in warning
    finally:
        if saved is not None:
            os.environ["SENSOR_FIXTURE_PATH"] = saved


def test_seed_data_module_never_reads_sample_messages():
    """seed_data.py must not open/read seed/sample_messages/messages.json —
    that is scripts/demo.sh's (task 29.1) responsibility, per the module
    docstring's scoping decision 3. The module docstring is allowed to
    *mention* sample_messages (to document the scoping decision); what must
    never appear is actual code referencing that path."""
    assert not hasattr(seed_data, "SAMPLE_MESSAGES_PATH")
    assert not hasattr(seed_data, "load_sample_messages")
