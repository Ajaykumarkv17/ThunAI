"""Idempotent seed helper for `scripts/seed.sh` (task 11.5, Req 19.7, 8.1a).

Invoked as ``python scripts/seed_data.py`` by the thin bash entrypoint
`scripts/seed.sh`. Populates the deployed account with the committed demo
fixtures. Requires no interactive input (Req 19.7) and is safe to re-run any
number of times, converging to the same state every time.

Verification note (mandatory "verify first" step, per tasks.md 11.5):
    Confirmed the current `bedrock-agent` `StartIngestionJob` API shape
    against the AWS documentation MCP server (`API_agent_StartIngestionJob.html`,
    matches `boto3==1.43.90` as pinned in `pyproject.toml`/`requirements.txt`):

    - The operation is ``PUT /knowledgebases/{knowledgeBaseId}/datasources/
      {dataSourceId}/ingestionjobs/``; the boto3 call is
      ``client("bedrock-agent").start_ingestion_job(knowledgeBaseId=...,
      dataSourceId=..., clientToken=..., description=...)`` — both
      ``knowledgeBaseId`` and ``dataSourceId`` are required path parameters,
      exactly the shape the task description assumed.
    - ``clientToken`` is documented as an *idempotency* token: "if this token
      matches a previous request, Amazon Bedrock ignores the request, but
      does not return an error" (min length 33, max length 256, pattern
      ``[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}``). This module therefore always
      passes a **deterministic** `clientToken` derived from the knowledge
      base id + data source id (not a random UUID) so that re-running
      `seed.sh` against the same deployed KB/data-source pair on the same day
      does not repeatedly enqueue duplicate ingestion jobs — it degrades to a
      no-op past the first call of the day, which is exactly the "repeatable
      to the same state" contract Req 19.7 asks for. (Bedrock's own
      idempotency window for a given token is not documented as indefinite,
      so the token includes the UTC date, which is a deliberate, documented
      choice: a fresh ingestion job is allowed once per day, not on every
      single invocation within the same day, while still allowing a genuinely
      new day's re-run to pick up doc edits.)
    - The response is a 202 with an ``ingestionJob`` object carrying
      ``ingestionJobId``/``status`` — this module does not poll for
      completion (Req 19.7 only requires *starting* the ingestion job; polling
      to completion is out of scope for a script that must run in one command
      with no interactive wait loop).
    - Errors this module treats as *expected, not fatal* (Bedrock is already
      ingesting the same data source): ``ConflictException`` — the API
      reference documents this as "There was a conflict performing an
      operation," which is exactly what firing a second `StartIngestionJob`
      call against a data source that already has one in progress produces.
      Any other `ClientError` propagates and fails the script.

Scoping decisions this module (deliberately) does NOT implement, each
documented so a future task's implementer knows where responsibility lives:

1. **Resident points are not written to any State_Store row.**
   `schemas/entities.py` (task 2.2) declares no persisted entity model for an
   individual resident point, and `memory/state_store.py` (task 6.1) exposes
   no corresponding write helper — only `put_incident`/`put_responder`/
   `put_shelter`/`put_escalation_record`. `seed/README.md`'s own text frames
   `resident_points` as "aggregate display data... used only for aggregate
   affected-area counts (Req 14.5) — never individually exposed," not a
   modeled State_Store entity. Rather than inventing a write path that
   neither the design nor the schema/state_store layer defines, this module
   loads `seed_dataset.json["resident_points"]` only to report its count in
   the run summary; a future frontend task (24.1, Resident_Status_Page) may
   choose to serve these points as a static asset instead, which is a
   decision for that task, not this script.
2. **`seed/hazard_readings/*.jsonl` is never uploaded anywhere.** Per
   `seed/hazard_readings/README.md`, these files are read directly off disk
   by `SyntheticSensorProvider` (task 10.1) at the path named by
   `SENSOR_FIXTURE_PATH` — there is no live store for hazard readings to
   write into (the sensor feed is ThunAI's one and only synthetic backend,
   Req 19.5). This module does not touch that env var's *value* (it is a
   static path already defaulted in `.env.example`); it only verifies the
   directory the deployer's environment already points at exists, and warns
   (without failing) if `SENSOR_FIXTURE_PATH` is unset.
3. **`seed/sample_messages/messages.json` is never loaded by this script.**
   Per `seed/sample_messages/README.md` and design.md §3.9, these fixtures
   are "fed through the real ingestion endpoint during the demo" — i.e. they
   are POSTed to the live API Gateway `Ingestion_Endpoint` by `scripts/demo.sh`
   (task 29.1, not yet implemented), not pre-loaded into any store by
   `seed.sh`. This module deliberately does not read this file, so a future
   implementer of task 29.1 knows that responsibility has not been taken by
   this script.

Env vars this module reads (all already declared in `.env.example` except
the two KB-ingestion ones this task adds — see the accompanying `.env.example`
diff):
    AWS_REGION                      region for every boto3 client (default us-west-2)
    THUNAI_STATE_TABLE              thunai-state DynamoDB table name (memory/state_store.py default)
    THUNAI_KNOWLEDGE_BASE_ID        Bedrock Knowledge Base id (required for step 2)
    THUNAI_KB_DATA_SOURCE_ID        Bedrock KB data source id (required for step 2)
    THUNAI_KNOWLEDGE_DOCS_BUCKET    S3 bucket backing that data source (required for step 2)
    SENSOR_FIXTURE_PATH             informational check only (see scoping decision 2 above)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

# Allow running this script directly (``python scripts/seed_data.py``) as
# well as importing it as a module (``python -m scripts.seed_data``) from the
# repository root, mirroring the sys.path convention already used by
# seed/_generate_seed_dataset.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory import state_store  # noqa: E402  (see sys.path insert above)
from schemas.entities import Responder, Shelter  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SEED_DATASET_PATH = REPO_ROOT / "seed" / "seed_dataset.json"
KNOWLEDGE_DOCS_DIR = REPO_ROOT / "seed" / "knowledge_docs"


class SeedStepError(RuntimeError):
    """Raised with a step name so main() can report "the first failed step"
    (per the task's summary/exit-code requirement), never swallowed."""

    def __init__(self, step: str, detail: str) -> None:
        self.step = step
        self.detail = detail
        super().__init__(f"[{step}] {detail}")


# ---------------------------------------------------------------------------
# Step 1 — responders & shelters into thunai-state (idempotent last-write-wins)
# ---------------------------------------------------------------------------


def load_seed_dataset() -> dict[str, Any]:
    if not SEED_DATASET_PATH.exists():
        raise SeedStepError(
            "load_seed_dataset",
            f"{SEED_DATASET_PATH} does not exist; run "
            "`python seed/_generate_seed_dataset.py` first (task 11.1).",
        )
    with SEED_DATASET_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def seed_responders_and_shelters(dataset: dict[str, Any]) -> tuple[int, int]:
    """Write every responder/shelter row via `memory.state_store`'s
    idempotent-safe `put_*` helpers (last-write-wins `put_item`, Req 15.1),
    so re-running this step any number of times converges on the exact same
    rows every time (Req 19.7's "repeatable to the same state").
    """
    responder_count = 0
    for raw in dataset.get("responders", []):
        responder = Responder(**raw)
        try:
            state_store.put_responder(responder)
        except Exception as exc:  # pragma: no cover - defensive, re-raised as SeedStepError
            raise SeedStepError(
                "seed_responders_and_shelters",
                f"failed writing responder {responder.responder_id!r}: {exc}",
            ) from exc
        responder_count += 1

    shelter_count = 0
    for raw in dataset.get("shelters", []):
        shelter = Shelter(**raw)
        try:
            state_store.put_shelter(shelter)
        except Exception as exc:  # pragma: no cover
            raise SeedStepError(
                "seed_responders_and_shelters",
                f"failed writing shelter {shelter.shelter_id!r}: {exc}",
            ) from exc
        shelter_count += 1

    return responder_count, shelter_count


# ---------------------------------------------------------------------------
# Step 2 — knowledge docs: upload to S3, then start KB ingestion job
# ---------------------------------------------------------------------------


def _iter_knowledge_doc_files() -> list[Path]:
    """Every `*.md` file in `seed/knowledge_docs/`, excluding `README.md`
    per that directory's own documented exclusion."""
    if not KNOWLEDGE_DOCS_DIR.is_dir():
        raise SeedStepError(
            "upload_knowledge_docs",
            f"{KNOWLEDGE_DOCS_DIR} does not exist.",
        )
    return sorted(
        p
        for p in KNOWLEDGE_DOCS_DIR.glob("*.md")
        if p.name.lower() != "readme.md"
    )


def upload_knowledge_docs(bucket: str, *, s3_client: Any) -> int:
    """Upload every non-README `seed/knowledge_docs/*.md` file to `bucket`,
    keyed by filename. Re-uploading the same file content to the same key is
    itself idempotent (S3 `PutObject` simply overwrites), so re-running this
    step converges to the same bucket contents every time."""
    files = _iter_knowledge_doc_files()
    for path in files:
        try:
            s3_client.upload_file(str(path), bucket, path.name)
        except ClientError as exc:
            raise SeedStepError(
                "upload_knowledge_docs",
                f"failed uploading {path.name!r} to s3://{bucket}/{path.name}: {exc}",
            ) from exc
    return len(files)


def _ingestion_client_token(knowledge_base_id: str, data_source_id: str) -> str:
    """Deterministic per-day idempotency token (see module docstring's
    verification note): allows one genuinely new ingestion job per UTC day
    for a given KB/data-source pair, and makes every other same-day re-run of
    this script a no-op against StartIngestionJob rather than enqueueing a
    duplicate job."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    raw = f"thunai-seed-{knowledge_base_id}-{data_source_id}-{day}"
    # Pattern: [a-zA-Z0-9](-*[a-zA-Z0-9]){0,256} — strip anything outside
    # that alphabet defensively (KB/data-source ids are alphanumeric per
    # their own [0-9a-zA-Z]{10} pattern, so this is a safety net, not an
    # expected transformation).
    return "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw)


def start_kb_ingestion_job(
    knowledge_base_id: str,
    data_source_id: str,
    *,
    bedrock_agent_client: Any,
) -> str:
    """Start (or, on a same-day re-run, no-op against) the S3 Vectors
    Knowledge Base ingestion job over the uploaded `seed/knowledge_docs/`
    files, via `bedrock-agent.start_ingestion_job` (verified shape, see
    module docstring)."""
    client_token = _ingestion_client_token(knowledge_base_id, data_source_id)
    try:
        response = bedrock_agent_client.start_ingestion_job(
            knowledgeBaseId=knowledge_base_id,
            dataSourceId=data_source_id,
            clientToken=client_token,
            description="ThunAI seed.sh — ingest seed/knowledge_docs/",
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConflictException":
            # Expected: an ingestion job for this data source is already
            # running (e.g. a very recent prior seed.sh run). Not a failure
            # of this script's contract — re-running still converges.
            return "conflict-ingestion-already-running"
        raise SeedStepError(
            "start_kb_ingestion_job",
            f"start_ingestion_job failed (knowledgeBaseId={knowledge_base_id!r}, "
            f"dataSourceId={data_source_id!r}): {exc}",
        ) from exc
    job = response.get("ingestionJob", {})
    return job.get("ingestionJobId", "unknown")


# ---------------------------------------------------------------------------
# Step 3 — informational check only: SENSOR_FIXTURE_PATH (no upload — see
# module docstring's scoping decision 2)
# ---------------------------------------------------------------------------


def check_sensor_fixture_path() -> str | None:
    """Return a warning string if `SENSOR_FIXTURE_PATH` is unset or does not
    exist on disk; return None if everything looks fine. Never raises — this
    is informational only, since hazard readings are read locally by
    `SyntheticSensorProvider`, not written anywhere by this script."""
    fixture_path = os.environ.get("SENSOR_FIXTURE_PATH")
    if not fixture_path:
        return "SENSOR_FIXTURE_PATH is unset; SyntheticSensorProvider will fall back to its own default."
    resolved = (REPO_ROOT / fixture_path) if not Path(fixture_path).is_absolute() else Path(fixture_path)
    if not resolved.is_dir():
        return f"SENSOR_FIXTURE_PATH={fixture_path!r} does not resolve to an existing directory."
    return None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> int:
    warnings: list[str] = []

    try:
        dataset = load_seed_dataset()
        responder_count, shelter_count = seed_responders_and_shelters(dataset)
        resident_point_count = len(dataset.get("resident_points", []))

        region = os.environ.get("AWS_REGION", "us-west-2")
        bucket = os.environ.get("THUNAI_KNOWLEDGE_DOCS_BUCKET", "")
        knowledge_base_id = os.environ.get("THUNAI_KNOWLEDGE_BASE_ID", "")
        data_source_id = os.environ.get("THUNAI_KB_DATA_SOURCE_ID", "")

        missing = [
            name
            for name, value in (
                ("THUNAI_KNOWLEDGE_DOCS_BUCKET", bucket),
                ("THUNAI_KNOWLEDGE_BASE_ID", knowledge_base_id),
                ("THUNAI_KB_DATA_SOURCE_ID", data_source_id),
            )
            if not value
        ]
        if missing:
            raise SeedStepError(
                "upload_knowledge_docs",
                "missing required environment variable(s): " + ", ".join(missing),
            )

        s3_client = boto3.client("s3", region_name=region)
        uploaded_count = upload_knowledge_docs(bucket, s3_client=s3_client)

        bedrock_agent_client = boto3.client("bedrock-agent", region_name=region)
        ingestion_job_id = start_kb_ingestion_job(
            knowledge_base_id, data_source_id, bedrock_agent_client=bedrock_agent_client
        )

        sensor_warning = check_sensor_fixture_path()
        if sensor_warning:
            warnings.append(sensor_warning)

    except SeedStepError as exc:
        print(f"ERROR: seed.sh failed at step '{exc.step}': {exc.detail}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - unexpected failure
        print(f"ERROR: seed.sh failed with an unexpected error: {exc!r}", file=sys.stderr)
        return 1

    print("ThunAI seed.sh summary:")
    print(f"  responders written:      {responder_count}")
    print(f"  shelters written:        {shelter_count}")
    print(f"  resident points (info):  {resident_point_count} (not written to State_Store — see module docstring)")
    print(f"  knowledge docs uploaded: {uploaded_count}")
    print(f"  KB ingestion job id:     {ingestion_job_id}")
    print("  sample_messages/:        not loaded by seed.sh (fed through the real ingestion endpoint by demo.sh)")
    if warnings:
        print("  warnings:")
        for warning in warnings:
            print(f"    - {warning}")
    print("Seed complete. Safe to re-run at any time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
