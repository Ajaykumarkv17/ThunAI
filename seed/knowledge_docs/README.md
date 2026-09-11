# Knowledge Docs Seed Fixtures

This directory contains the curated community flood-safety documents that
`scripts/seed.sh` (task 11.5) ingests into the live Amazon Bedrock Knowledge
Base backed by an Amazon S3 Vectors vector store (see `infra/stacks/knowledge_stack.py`,
task 21.3a, and design.md §3.11 "KnowledgeStack"). `tools/knowledge_tools.py::retrieve_passages`
(task 12.6) and `Knowledge_Agent` (task 13.7) query this index at runtime to
produce grounded, cited answers to resident safety questions (Req 8.1a, 8.2,
8.3).

**These documents are synthetic-but-realistic content authored for demo
purposes.** They are not officially issued government guidance, and they must
not be presented to residents, judges, or anyone else as authoritative
official communication. Any place a real phone number or organisation name
would normally appear, a clearly-labelled placeholder is used instead (for
example `[LOCAL EMERGENCY HELPLINE NUMBER]`) so nobody mistakes a demo
document for a live emergency contact.

## Topics covered

| Topic | English file | Tamil file |
|---|---|---|
| Water safety | `water_safety_en.md` | `water_safety_ta.md` |
| Shelter guidance | `shelter_guidance_en.md` | `shelter_guidance_ta.md` |
| Evacuation kit / what to take | `evacuation_kit_en.md` | `evacuation_kit_ta.md` |
| Evacuation basics | `evacuation_basics_en.md` | `evacuation_basics_ta.md` |
| After the flood: returning home safely | `after_flood_en.md` | `after_flood_ta.md` |

Five topics, ten files total (one English file and one Tamil file per topic).

## File-naming and language convention

Each topic is split into **two separate files, one per language**, rather
than one bilingual file with mixed sections. This is the cleaner shape for a
vector-KB ingestion pipeline (task 21.3a's Bedrock Knowledge Base data source)
that treats each ingested file as one retrievable document: a resident's
question in Tamil should retrieve a passage written entirely in Tamil, not a
passage that mixes both languages in one chunk.

Naming convention: `<topic>_<lang>.md`, where `<lang>` is `en` (English) or
`ta` (Tamil) and `<topic>` matches the topic column above (`water_safety`,
`shelter_guidance`, `evacuation_kit`, `evacuation_basics`, `after_flood`).

## `source_version` convention

Every document opens with a YAML front-matter block:

```markdown
---
title: <document title>
topic: <topic slug>
language: en|ta
source_version: 2026-09-v1
---
```

`source_version` is the grounding-traceability tag `retrieve_passages` and
`Knowledge_Agent` surface per citation (Req 8.2: "each citation carrying the
passage identifier and the knowledge source version") and per Req 18.6's
data-minimisation/versioning expectations. All documents in this seed batch
share the same `source_version` (`2026-09-v1`) because they were authored and
committed together; a future content revision should bump this value (for
example to `2026-10-v1`) for the specific file(s) changed, not for the whole
batch, so citations can distinguish which version of a document informed a
given answer.

## Content scope

Per design.md §3.9, each document is several paragraphs/bullets of realistic,
plain-language safety guidance appropriate to a river-flood emergency in a
Tamil/Indian setting — referencing practical realities such as power cuts,
mobile network congestion, and local shelter norms. Advisory-category content
(anything touching medical treatment or structural safety) carries an
explicit disclaimer and a referral to a qualified authority rather than a
directive to self-treat or self-repair, consistent with Req 8.6/18.6's
advisory-disclaimer requirement.

## Ingestion

`scripts/seed.sh` (task 11.5, not yet implemented) starts the S3 Vectors
Knowledge Base ingestion job over this directory against the deployed
account. There is no synthetic backend for the Knowledge Base itself (Req
19.5): these are synthetic *documents* written into a real, live AWS
Knowledge Base, exactly as design.md §3.9 describes for all seed content
other than the hazard-sensor readings.
