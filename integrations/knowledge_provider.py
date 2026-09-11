"""Knowledge_Provider interface and its one, live-only implementation (Req 8.1, 8.1a).

`Knowledge_Base` (design.md §3.11 "KnowledgeStack") is an Amazon Bedrock Knowledge
Base whose vector index is an Amazon S3 Vectors vector store. Per design.md §3.7
("Backend seam — real by default, synthetic only for the sensor feed") and the
resolved Open Question 6, `Knowledge_Provider` is queried through the live
`bedrock-agent-runtime` `Retrieve` API in every environment. Unlike
`Sensor_Provider` (the ONLY interface in ThunAI_Platform with a synthetic
backend), there is deliberately no synthetic/fake `KnowledgeProvider`
implementation in this module — that omission is the point of the task title
("LIVE ONLY — S3 Vectors KB, no synthetic backend"), not an oversight.

`.env.example` is explicit that `THUNAI_KNOWLEDGE_BASE_ID` is queried "via the
live bedrock-agent-runtime Retrieve API (the deprecated strands-agents-tools
`retrieve` tool is NOT used)" — this module calls `boto3.client("bedrock-agent-
runtime").retrieve(...)` directly, never the `strands_tools.retrieve` tool.

Verification notes (per the established verification-note convention used
elsewhere in this codebase, e.g. design.md's numbered Open Questions / Risks):

1. **Request shape — confirmed against the current `Retrieve` API reference**
   (`API_agent-runtime_Retrieve.html`, boto3 1.43.90 matches this shape):
   the operation is `POST /knowledgebases/{knowledgeBaseId}/retrieve`, and the
   boto3 call is
   ``client.retrieve(knowledgeBaseId=..., retrievalQuery={"text": ...},
   retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": ...}})``
   — exactly the shape design.md's `BedrockKBKnowledgeProvider.retrieve()`
   pseudocode assumed. `retrievalConfiguration` also accepts a
   `managedSearchConfiguration` sibling key (reranking / metadata-filtered
   search) that this module does not use, since ThunAI's curated document set
   is small and does not need reranking.
2. **Response shape — confirmed, with one correction to design.md's
   pseudocode.** `retrievalResults` is a list of `KnowledgeBaseRetrievalResult`
   objects, each with `content` (an object; the plain-text case is
   `content.text`), `location` (a *type-tagged union* object — e.g.
   `location.s3Location.uri` for an S3-sourced document, not a bare string —
   design.md's pseudocode wrote `r["location"]` as if it were already a
   string, which this module corrects by resolving the union to a string),
   `metadata` (a free-form `dict[str, JSON value]` of custom document-level
   metadata), `score` (a float), and `documentId` (a string). All five keys
   exist at the nesting the API reference describes; none are undocumented.
3. **`source_version` survival into `metadata` — confirmed as *possible but
   not guaranteed by this module*, per the Bedrock Knowledge Base S3 data
   source "document metadata files" (`.metadata.json` sidecar) documentation.**
   Custom per-document metadata attributes (such as the `source_version` field
   `seed/knowledge_docs/README.md` documents in each document's YAML front
   matter) survive into a retrieval result's `metadata` dict **only if** the
   knowledge base's ingestion pipeline (task 21.3a `knowledge_stack.py` /
   task 11.5 `scripts/seed.sh`, neither implemented as of this task) attaches
   a `.metadata.json` sidecar file per document carrying that same key, or
   configures inline metadata at ingestion time. The `Retrieve` API itself
   does not parse a document's own front matter — front matter is plain text
   as far as the API is concerned. This module therefore does NOT assume
   `metadata["source_version"]` is always present (design.md's pseudocode
   assumed an unconditional `r["metadata"]["source_version"]` lookup, which
   would raise `KeyError` the first time ingestion has not yet wired up the
   sidecar file). Instead, `Passage.source_version` is `None` when the key is
   absent, and this gap is surfaced in this module's docstring for task 21.3a/
   11.5 to close by emitting a `<file>.metadata.json` sidecar (or inline
   `DocumentMetadata`) carrying `source_version` alongside each ingested file.
4. **`moto` coverage for `bedrock-agent-runtime` — confirmed absent.** As of
   `moto==5.2.3` (the version pinned in `pyproject.toml`'s `test` extra),
   `moto`'s supported service list does not include `bedrock-agent-runtime`
   (only `bedrock` and `bedrock-runtime` have partial mock coverage). Tests in
   `tests/unit/test_knowledge_provider.py` therefore use a constructor-injected
   `unittest.mock.MagicMock` in place of a real boto3 client rather than
   `moto`, consistent with this module's own recommendation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

import boto3


class KnowledgeProviderConfigurationError(RuntimeError):
    """Raised at construction time when a required live-resource identifier is
    absent, per design.md §3.7's live-resource/credential-absent abort pattern
    (task 10.4 will formalise this pattern across every provider module; this
    provider applies it to itself now rather than sending a request with an
    empty knowledge base id to the live Retrieve API).
    """


@dataclass(frozen=True)
class Passage:
    """One retrieved passage, typed for a caller (`tools/knowledge_tools.py::
    retrieve_passages`, task 12.6, and `Knowledge_Agent`, task 13.7 — neither
    implemented yet) to produce a grounded, cited answer (Req 8.1, 8.2).

    Attributes:
        text: The passage content (`content.text` from the API response).
        score: The relevance score attached to this result by the knowledge
            base's search (Req 8.1: "each retrieved passage carrying a
            relevance score").
        source_id: A source/location identifier for citation, resolved from
            the type-tagged `location` object (e.g. the S3 URI for an
            S3-sourced document). Falls back to `documentId`, then to the
            literal string ``"unknown"``, if no recognised location type is
            present.
        source_version: The document-level `source_version` metadata tag
            (`seed/knowledge_docs/README.md`'s front-matter convention), used
            for citation per Req 8.2 ("each citation carrying the passage
            identifier and the knowledge source version"). `None` when the
            retrieval result's `metadata` dict carries no `source_version`
            key — see verification note 3 above; this is a real gap this
            provider cannot close on its own, since it can only return
            whatever the live API surfaces.
        document_id: The `documentId` field from the API response, when
            present.
        metadata: The raw `metadata` dict from the API response, unfiltered,
            so a caller can look up any other custom attribute the ingestion
            pipeline attached beyond `source_version`.
    """

    text: str
    score: float
    source_id: str
    source_version: str | None
    document_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class KnowledgeProvider(Protocol):
    """The interface supplying grounded, cited passage retrieval from
    Knowledge_Base (Req 8.1a). Implemented against the live Bedrock Knowledge
    Base in every environment — there is no synthetic backend for this
    interface (design.md §3.7, design principle 5).
    """

    def retrieve(self, query: str, *, max_results: int = 5) -> list[Passage]:
        """Retrieve up to `max_results` relevant passages for a free-text query.

        Args:
            query: The resident's safety question, or any other free-text
                query to search the knowledge base with.
            max_results: The maximum number of passages to return. Callers
                enforce Req 8.1's "at most the configured maximum passage
                count" by passing their own configured value here; this
                interface applies no policy of its own beyond the default.

        Returns:
            A list of `Passage` objects, ordered by descending relevance
            score as returned by the knowledge base's search, of length at
            most `max_results`.
        """
        ...


class BedrockKBKnowledgeProvider:
    """Queries the Bedrock Knowledge Base (Amazon S3 Vectors vector store) via
    the live `bedrock-agent-runtime` `Retrieve` API. This is the ONLY
    `KnowledgeProvider` implementation in ThunAI_Platform (LIVE ONLY, no
    synthetic backend, per the resolved Open Question 6 and design.md §3.7) —
    the demo shows real retrieval-augmented generation against the populated
    S3 Vectors index, never a fixture.

    Fails fast at construction time (not at first `retrieve()` call) when
    `knowledge_base_id` is empty, so a misconfigured deployment cannot send a
    request to the live Retrieve API with an empty knowledge base id.
    """

    def __init__(
        self,
        knowledge_base_id: str,
        *,
        region_name: str | None = None,
        client: Any | None = None,
    ) -> None:
        """Construct a live Bedrock Knowledge Base provider.

        Args:
            knowledge_base_id: The target knowledge base id (from
                `THUNAI_KNOWLEDGE_BASE_ID`, a stack output of the deployed
                `KnowledgeStack`, per `.env.example`). Must be non-empty.
            region_name: The AWS region to construct the boto3 client in.
                Defaults to `AWS_REGION` (falling back to `us-west-2`), matching
                every other boto3 client in this codebase (`agents/config.py`).
            client: An optional pre-built `bedrock-agent-runtime` boto3 client,
                for dependency injection in tests. When omitted, a real client
                is constructed via `boto3.client("bedrock-agent-runtime", ...)`.
        """
        if not knowledge_base_id:
            raise KnowledgeProviderConfigurationError(
                "BedrockKBKnowledgeProvider requires a non-empty knowledge_base_id "
                "(THUNAI_KNOWLEDGE_BASE_ID is unset or empty). Refusing to query "
                "the live Bedrock Knowledge Base Retrieve API with no target "
                "knowledge base id — set THUNAI_KNOWLEDGE_BASE_ID to the "
                "KnowledgeStack's deployed knowledge base id."
            )
        self._knowledge_base_id = knowledge_base_id
        self._client = client or boto3.client(
            "bedrock-agent-runtime",
            region_name=region_name or os.environ.get("AWS_REGION", "us-west-2"),
        )

    def retrieve(self, query: str, *, max_results: int = 5) -> list[Passage]:
        """Call the live `Retrieve` API and map its response into `Passage` objects.

        See this module's docstring (verification notes 1-2) for the exact
        request/response shape this call relies on.
        """
        response = self._client.retrieve(
            knowledgeBaseId=self._knowledge_base_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {"numberOfResults": max_results}
            },
        )
        return [
            self._to_passage(result) for result in response.get("retrievalResults", [])
        ]

    @staticmethod
    def _to_passage(result: dict[str, Any]) -> Passage:
        content = result.get("content") or {}
        metadata = result.get("metadata") or {}
        document_id = result.get("documentId")
        return Passage(
            text=content.get("text", ""),
            score=float(result.get("score", 0.0)),
            source_id=BedrockKBKnowledgeProvider._location_to_str(
                result.get("location") or {}, document_id
            ),
            source_version=metadata.get("source_version"),
            document_id=document_id,
            metadata=dict(metadata),
        )

    @staticmethod
    def _location_to_str(location: dict[str, Any], document_id: str | None) -> str:
        """Resolve the type-tagged `location` union to a single citation string.

        `location` carries a `type` discriminator plus exactly one populated
        `<type>Location` sub-object (e.g. `s3Location.uri`); see verification
        note 2. This helper checks each documented sub-object key in turn and
        returns the first populated identifying value, falling back to
        `documentId`, then to the literal string ``"unknown"``.
        """
        for key, field_name in (
            ("s3Location", "uri"),
            ("webLocation", "url"),
            ("confluenceLocation", "url"),
            ("salesforceLocation", "url"),
            ("sharePointLocation", "url"),
            ("oneDriveLocation", "url"),
            ("googleDriveLocation", "url"),
            ("kendraDocumentLocation", "uri"),
            ("customDocumentLocation", "id"),
            ("sqlLocation", "query"),
        ):
            sub_object = location.get(key)
            if sub_object and sub_object.get(field_name):
                return str(sub_object[field_name])
        return document_id or "unknown"


def get_knowledge_provider() -> KnowledgeProvider:
    """Factory returning the one live `KnowledgeProvider` implementation.

    `Knowledge_Provider` has no synthetic backend (design principle 5;
    `Synthetic_Sensor_Flag` affects `Sensor_Provider` only, per Req 19.5), so
    this factory always returns `BedrockKBKnowledgeProvider` — there is no
    flag to branch on here. It exists only for the uniform `get_X_provider()`
    factory shape used across `integrations/` (mirroring
    `sensor_provider.py::get_sensor_provider()`), so callers do not need to
    know which providers happen to have more than one implementation.
    """
    return BedrockKBKnowledgeProvider(
        knowledge_base_id=os.environ.get("THUNAI_KNOWLEDGE_BASE_ID", ""),
        region_name=os.environ.get("AWS_REGION", "us-west-2"),
    )
