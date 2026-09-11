r"""Field-name-based redaction of PII/secret fields before persistence or emission (Req 16.10, 20.2).

`design.md` §3.6 specifies `redact_fields()` in exactly these terms:

    `redact_fields()` (Req 16.10) walks a configured field-name list
    (`resident_name`, `resident_contact`, `resident_address`, any key matching
    `*_secret`, `*_credential`, `*_api_key`) recursively through the input dict
    and replaces matched values with `"[REDACTED]"` before the dict ever
    reaches a log call, a trace attribute, or `append_audit_entry` — applied
    at the hook layer so no individual tool author can forget it (this is the
    mechanism, independent per-call from any tool's own code, that makes
    redaction "complete" in the correctness-property sense of §7).

That is a **field-name** matching rule, stated with no mention of scanning
string *values* for PII/secret-shaped content. Design decision made here,
stated explicitly rather than left implicit: `redact_fields()` implements
field-name matching only, and does NOT additionally run
`policy.safety_policy.detect_pii_or_secrets()` (or `PII_SECRET_PATTERNS`)
over remaining string values. Rationale:

- §3.6's own text names the mechanism precisely ("a configured field-name
  list ... walks ... recursively"); it does not say "and also scans values."
  Where design.md gives an exact algorithm, this module follows it exactly
  rather than expanding scope.
- `detect_pii_or_secrets()` already has a distinct, documented call site and
  purpose: `Safety_QA_Agent`'s policy review of *proposed outbound content*
  (Req 9.3), which populates `SafetyReview.violated_policy_ids` — a review
  decision the model/pipeline reasons about, not a blanket redaction of
  arbitrary internal tool-call inputs. Folding value-content scanning into
  `redact_fields()` would silently mutate tool-call inputs/outputs that
  Safety_QA_Agent (or a human reading the audit ledger) needs to inspect
  faithfully outside the three declared PII/secret field names, and would
  duplicate/entangle two independently-versioned policies
  (`SAFETY_POLICY_VERSION` vs. whatever this module's field list would need)
  for no requirement that asks for it.
- Field-name redaction is also the only approach that can satisfy the
  "redaction completeness" correctness property (Req 16.10, Property 15) as
  a *simple, total* guarantee: every value under a matched key is always
  replaced, with no false-negative risk from a regex failing to match a
  PII shape it wasn't written for. Value-content scanning is inherently
  best-effort (regexes miss shapes) and would weaken that guarantee if it
  were the sole layer.

If a future task decides defence-in-depth value scanning is also wanted at
one or both call sites, that is a separate, additively-composed step (e.g. a
second pass calling `detect_pii_or_secrets()` on remaining string leaves) —
not a change to this module's documented field-name-only contract.

Call sites (per design.md §3.2's `Incident_Graph` driver and task 8.3's
"both call sites" framing):

1. `harness/audit.py::append_audit_entry` — redacts a tool call's `inputs`
   dict before it is persisted to `Audit_Ledger` (Req 16.10).
2. A future observability/tracing call site (task 22.1, not yet implemented)
   that redacts span attributes before emission (Req 20.2: "SHALL record no
   resident free-text content and no credential value in any span
   attribute").

Both call sites need the same shape: take an arbitrary nested structure
(dict / list / scalar) and get back a structurally-identical value with only
the matched leaf values replaced. `redact_fields()` is therefore a pure
function with no side effects and no dependency on either caller's specific
data shape beyond "arbitrary nested dict/list/scalar structure" — safe to
call from both.

Verification note (mandatory per tasks.md 8.1): this module is pure Python
recursion over `dict`/`list`/scalar structures with no external SDK surface
to verify. The recursive traversal:
- descends into `dict` values (checking each key against the redacted
  field-name rule and either replacing the value outright or recursing into
  it if it is itself a dict/list),
- descends into `list`/`tuple` elements (each element is processed by the
  same recursive step, so a list of dicts has each dict's matched keys
  redacted, and a list of plain scalars passes through unchanged),
- passes through every other leaf value (`str` that doesn't matter because
  it's not under a matched key, `int`, `float`, `bool`, `None`, or any other
  object) completely unchanged, since matching is keyed on the *dict key*,
  never on the value's own type or content.
`None`, numbers, and booleans need no special-casing: they simply fall out
of the "is this a dict or list" checks as an unchanged leaf, same as an
ordinary non-matching string.
"""

from __future__ import annotations

from typing import Any, Final

# ---------------------------------------------------------------------------
# The configured field-name list (design.md §3.6, Req 16.10).
# ---------------------------------------------------------------------------

#: Exact dict-key matches that must always be redacted, regardless of nesting
#: depth or container (top-level dict, nested dict, or dict inside a list).
EXACT_REDACTED_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "resident_name",
        "resident_contact",
        "resident_address",
    }
)

#: Suffixes that mark a dict key as redacted regardless of its prefix, e.g.
#: `db_password_secret`, `sns_credential`, `third_party_api_key` all match.
REDACTED_FIELD_NAME_SUFFIXES: Final[tuple[str, ...]] = (
    "_secret",
    "_credential",
    "_api_key",
)

#: The fixed, non-identifying placeholder every matched value is replaced
#: with. A placeholder is used (never key removal) so the redacted
#: structure's shape and key set stay inspectable in the audit ledger / a
#: trace attribute — only the sensitive value itself disappears, matching
#: Req 16.10's "replace the value ... with a non-identifying placeholder"
#: wording exactly (as opposed to a requirement to *remove* the field).
REDACTION_PLACEHOLDER: Final[str] = "[REDACTED]"


def _is_redacted_field_name(key: Any) -> bool:
    """Return True if `key` is a dict key that must be redacted.

    Exact match against `EXACT_REDACTED_FIELD_NAMES`, or a suffix match
    against `REDACTED_FIELD_NAME_SUFFIXES`. Non-string keys can never match
    (dict keys carrying PII/secrets are always strings in this codebase's
    schemas) and are treated as ordinary, non-matching keys.
    """
    if not isinstance(key, str):
        return False
    if key in EXACT_REDACTED_FIELD_NAMES:
        return True
    return any(key.endswith(suffix) for suffix in REDACTED_FIELD_NAME_SUFFIXES)


def redact_fields(data: Any) -> Any:
    """Recursively redact configured PII/secret fields in a nested structure.

    Walks `data` — which may be a `dict`, a `list`/`tuple`, or any scalar
    leaf value — and returns a new structure of the same shape in which every
    dict value whose key matches `EXACT_REDACTED_FIELD_NAMES` or
    `REDACTED_FIELD_NAME_SUFFIXES` is replaced with `REDACTION_PLACEHOLDER`,
    at any nesting depth, including inside dicts nested in lists.

    Non-matching keys, and any value that is not itself a dict/list needing
    further recursion, are returned unchanged (same object, for scalars).
    `None`, numbers, booleans, and plain strings under non-matching keys all
    pass through untouched.

    Args:
        data: An arbitrary nested structure — typically a tool call's
            `inputs` dict (the `harness/audit.py::append_audit_entry` call
            site) or a span-attribute dict (a future observability call
            site) — to redact before persistence or emission.

    Returns:
        A new structure of the same shape as `data`, with every matched
        field's value replaced by `"[REDACTED]"`. The original `data` is
        never mutated in place.
    """
    if isinstance(data, dict):
        redacted: dict[Any, Any] = {}
        for key, value in data.items():
            if _is_redacted_field_name(key):
                redacted[key] = REDACTION_PLACEHOLDER
            else:
                redacted[key] = redact_fields(value)
        return redacted

    if isinstance(data, list):
        return [redact_fields(item) for item in data]

    if isinstance(data, tuple):
        return tuple(redact_fields(item) for item in data)

    # Scalar leaf: str (under a non-matching key), int, float, bool, None,
    # or any other object. Matching is keyed on the dict key, never on the
    # leaf's own type or content, so every such value passes through
    # unchanged.
    return data
