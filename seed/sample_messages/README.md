# seed/sample_messages/

Synthetic inbound resident messages used to exercise Intake_Agent (task 13.3,
not yet implemented) and the eval suite (task 28.1, task 28.5 — Property 10
model-label check). No real PII: every `sender_reference` is a synthetic
identifier, no real names, phone numbers, or street addresses are used.

## File format

`messages.json` is a single JSON list of 8 message records. Each record has:

- `message_id` — unique identifier (`MSG-001` .. `MSG-008`).
- `sender_reference` — synthetic sender identifier, not a real contact.
- `source_language_hint` — a hint at the message's dominant language
  (`ta` = Tamil, `en` = English, `ta-en` = code-mixed Tanglish). This is a
  fixture-authoring hint only; Intake_Agent must still perform its own
  `source_language` detection against the message text rather than trusting
  this field, so the eval suite can check the model's own detection.
- `text` — the raw free-text message, in its original language(s). This is
  what gets POSTed to the real ingestion endpoint during the demo.
- `text_translation_note` — an English gloss of `text` for readers reviewing
  this fixture set, `null` when `text` is already in English. Not sent to the
  agent; documentation only.
- `expected` — the hand-labelled expected classification, using only field
  values valid per `schemas.decisions.EmergencyRequest`: `request_category`,
  `occupant_count`, `location_reference`, `mobility_assistance`,
  `medical_need`, `urgency_band`, `dispatch_eligible`. These are the labels
  task 28.5 will assert the real Intake_Agent output against.
- `expected_location_candidates_count_min` (only on `MSG-007`) — the minimum
  number of ranked location candidates Intake_Agent should record for the
  ambiguous-location fixture (Req 5.6).
- `expected_manual_triage` (only on `MSG-008`) — `true` on the off-topic
  fixture, indicating this message should route to
  `NON_DISPATCH_ELIGIBLE` → `MANUAL_TRIAGE` per `schemas/lifecycle.py`
  (Req 5.8), rather than receiving a `request_category` assignment.

## Language mix

- Tamil-dominant: `MSG-001`, `MSG-005`, `MSG-006`.
- Code-mixed Tanglish: `MSG-003`.
- English-dominant: `MSG-002`, `MSG-004`, `MSG-007`, `MSG-008`.

Both required `source_language` values (Tamil and English, per the
`Community_Language_Configuration`) are represented, satisfying Req 21.1's
"at least one intake scenario for each language" fixture-coverage need.

## What each message is designed to test

| Message ID | Category | Designed to test |
|---|---|---|
| `MSG-001` | RESCUE | Baseline RESCUE intake in Tamil, immediate mobility-independent rescue, occupant count stated. |
| `MSG-002` | MEDICAL | Baseline MEDICAL intake, always-ask category via `medical_need=True`, so `dispatch_eligible=False` pending escalation. |
| `MSG-003` | SHELTER | Tanglish code-mixed message; SHELTER category with an occupant count. |
| `MSG-004` | SUPPLIES | Routine, non-urgent SUPPLIES request; the ROUTINE urgency-band baseline. |
| `MSG-005` | INFORMATION | Pure information request with **no physical assistance requested** — must route to Knowledge_Agent, not create a dispatch-eligible request (Req 5.7). |
| `MSG-006` | RESCUE | **Mobility-assistance fixture.** Elderly, wheelchair-bound resident who cannot self-evacuate — `mobility_assistance=True` is the obviously correct label; always-ask category, so `dispatch_eligible=False`. |
| `MSG-007` | RESCUE | **Ambiguous-location fixture.** Landmark description with no clear street address and an explicit second, confusable landmark — tests Req 5.6's location-candidate-ranking behaviour; expects 2+ recorded candidates. |
| `MSG-008` | OTHER | **Off-topic / manual-triage fixture.** Not a real emergency request (unrelated school-admission and SIM-card questions) — expects no `request_category` match, routing to `MANUAL_TRIAGE` per Req 5.8. |

## Coverage summary

- Categories: RESCUE (×2), MEDICAL (×1), SHELTER (×1), SUPPLIES (×1),
  INFORMATION (×1), OTHER/manual-triage (×1).
- Exactly one mobility-assistance fixture: `MSG-006`.
- Exactly one ambiguous-location fixture: `MSG-007`.
- Exactly one off-topic/manual-triage fixture: `MSG-008`.
- Both Tamil and English represented as dominant `source_language_hint`.

## References

Requirements: 21.1 (eval scenario coverage — one intake per language), 5.6
(location resolution / ambiguous location), 5.8 (off-topic / manual-triage
routing). Design: §3.9 (seed dataset contents).
