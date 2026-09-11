"""Validation-failure guard for typed structured-output invocations (design.md §4.1).

`safe_structured()` wraps any callable that is expected to return a validated
Pydantic model (typically the result of an `Agent(...)` invocation made with
`structured_output_model=<one of schemas/decisions.py's five models>`, or a
direct `model_cls.model_validate(raw)` call site that does not go through an
`Agent` at all) and guarantees the caller always gets back *something* typed,
never a raised exception and never an untyped blob (Req 10.11).

Verification note (mandatory per tasks.md 2.4):
    - Re-confirmed, independently of the note already recorded in
      `schemas/decisions.py` (task 2.1), the current Strands Agents docs
      (structured-output.md) description of `structured_output_model=`:
      a successful call returns the validated instance on
      `AgentResult.structured_output`; when the model's response cannot be
      parsed/validated after the SDK's own internal retry budget is
      exhausted, the SDK raises `strands.types.exceptions.
      StructuredOutputException` — a plain `Exception` subclass carrying a
      single `.message: str` attribute, defined as:

          class StructuredOutputException(Exception):
              # "Exception raised when structured output validation fails
              # after maximum retry attempts."
              def __init__(self, message: str): ...

      This is a **deviation from design.md §4.1**, which describes
      `safe_structured()` as catching a Pydantic `ValidationError` directly.
    - Re-verified again after `requirements.txt`/`pyproject.toml` were
      updated to pin the latest available PyPI releases (per explicit user
      instruction): the environment's installed `strands-agents` package
      now resolves to `pip show strands-agents` version **1.55.0** (up
      from the `1.23.0` originally installed when this module was first
      written, and from the earlier design-pinned `1.54.0`). Re-imported
      `strands.types.exceptions.StructuredOutputException` and re-ran
      `inspect.getsource()` against the 1.55.0 install: the class is
      unchanged - same module path, same `Exception` base, same
      `__init__(self, message: str)` signature, same `.message`
      attribute. Also re-confirmed `Agent.__call__`'s signature still
      exposes `structured_output_model: type[pydantic.BaseModel] | None`
      as a keyword parameter. No code changes were required; this module
      imports the exception unconditionally, so if a future
      `strands-agents` release removes or renames it, the import below
      will raise `ImportError` at module load time, which is the correct
      fail-fast behaviour for a guard whose entire job is to catch a
      specific exception type.
    - `pydantic.ValidationError` is caught defensively in addition, for
      call sites that validate directly via `model_cls.model_validate(...)`
      without going through an `Agent` invocation at all (e.g. a future
      tool that parses a raw JSON payload). `StructuredOutputException`
      does not expose the raw invalid text as a separate attribute (only
      `.message`, which the SDK formats to include it), so `str(exc)` is
      used as the best-effort raw-output/error-detail capture for both
      exception types.

Design choice — fallback model shape (Req 10.11):
    The five decision models in `schemas/decisions.py` (`HazardAssessment`,
    `EmergencyRequest`, `DispatchDecision`, `AlertDraft`, `SafetyReview`) each
    carry additional *required* fields with no sensible generic default
    (e.g. `HazardAssessment.rate_of_change`, `EmergencyRequest.
    location_reference`). Constructing an instance of the original
    `model_cls` on a validation failure would therefore require inventing
    placeholder values for fields the guard has no way to know, which is
    exactly the kind of silent fabrication Req 10.11 is designed to
    prevent. Instead, `safe_structured()` returns a small, distinct
    `ValidationFailureFallback` model defined in this module. It carries
    only the fields Req 10.11 and the escalation policy (`policy.
    escalation_policy.decide()`, design §3.4) actually need to gate *any*
    decision uniformly (`confidence`, `action`, `category`,
    `is_irreversible_action`), plus `raw_output` and `error_detail` so the
    validation failure can be audited per Req 10.11's "record the
    validation failure in Audit_Ledger with the decision identifier".
    Callers that need a `model_cls`-shaped object on every path should
    check `isinstance(result, model_cls)` and treat any
    `ValidationFailureFallback` as the ask-human case.
"""

from typing import Callable, TypeVar

from pydantic import BaseModel, Field, ValidationError

from strands.types.exceptions import StructuredOutputException

ModelT = TypeVar("ModelT", bound=BaseModel)


class ValidationFailureFallback(BaseModel):
    """Synthetic ask-human decision returned when structured-output validation fails (Req 10.11).

    Carries the four fields (`confidence`, `action`, `category`,
    `is_irreversible_action`) every decision model in `schemas/decisions.py`
    also carries, so `policy.escalation_policy.decide()` can gate this
    fallback the same way it gates a real decision, plus the raw invalid
    output and error detail for `Audit_Ledger`.
    """

    confidence: float = Field(default=0.0, description="Always 0.0 for a validation failure.")
    action: str = Field(default="ask_human", description="Always 'ask_human' for a validation failure.")
    category: str = Field(default="validation_failure", description="Always 'validation_failure'.")
    is_irreversible_action: bool = Field(
        default=False, description="Withheld pending human response; not itself an irreversible action."
    )
    raw_output: str = Field(description="Best-effort text of the invalid output that failed validation.")
    error_detail: str = Field(description="The validation/structured-output exception message.")


def safe_structured(fn: Callable[[], ModelT], model_cls: type[ModelT]) -> ModelT | ValidationFailureFallback:
    """Call `fn()` and guarantee a typed result even on structured-output validation failure.

    Args:
        fn: A zero-argument callable that performs the structured-output
            invocation and returns the validated Pydantic instance on
            success — e.g. `lambda: agent(prompt, structured_output_model=model_cls).structured_output`
            for an `Agent` call site, or `lambda: model_cls.model_validate(raw)`
            for a direct-validation call site.
        model_cls: The decision model `fn` is expected to produce (used only
            to name the model in the error detail; not itself constructed
            here — see the "Design choice" note in this module's docstring).

    Returns:
        The validated instance returned by `fn()` on success, unchanged. On
        a `StructuredOutputException` or `pydantic.ValidationError`, a
        `ValidationFailureFallback` with `action="ask_human"`,
        `confidence=0.0`, `category="validation_failure"`,
        `is_irreversible_action=False`, and the raw invalid output /
        error detail populated for audit. Any other exception is not
        caught and propagates to the caller unchanged — only structured-
        output validation failures are treated as ask-human decisions
        (Req 10.11 names validation failure specifically; it does not
        extend to unrelated errors such as network failures, which the
        harness's own retry/cap hooks are responsible for).
    """
    try:
        return fn()
    except (StructuredOutputException, ValidationError) as exc:
        return ValidationFailureFallback(
            raw_output=str(exc),
            error_detail=f"{model_cls.__name__} structured-output validation failed: {exc}",
        )
