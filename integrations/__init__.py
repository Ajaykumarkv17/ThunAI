"""Integration seam: provider resolver plus synthetic/live sensor, notification, and knowledge providers.

Also performs the credential/resource-absent abort check at cold start (Req 19.13).

Provider factories (Req 19.5, design.md §3.7)
----------------------------------------------
Three provider modules already exist under ``integrations/``, each exposing
its own factory function:

- ``integrations/sensor_provider.py::get_sensor_provider()`` — the ONLY
  factory that reads ``SYNTHETIC_SENSORS``. Selects
  ``SyntheticSensorProvider`` (the default) or ``LiveSensorProvider``.
- ``integrations/notification_provider.py::notification_provider()`` — no
  flag; always returns the live ``SnsSesNotificationProvider``.
- ``integrations/knowledge_provider.py::get_knowledge_provider()`` — no
  flag; always returns the live ``BedrockKBKnowledgeProvider`` (raises
  ``KnowledgeProviderConfigurationError`` at construction time if
  ``THUNAI_KNOWLEDGE_BASE_ID`` is empty).

``SYNTHETIC_SENSORS`` selects the ``Sensor_Provider`` backend ONLY (Req
19.5): confirmed by construction, since ``sensor_provider.py`` is the only
module in this package that reads that environment variable at all — grep
confirms no other module under ``integrations/`` references
``SYNTHETIC_SENSORS``. :data:`SYNTHETIC_SENSORS_AFFECTS` records this fact
as an explicit, importable constant rather than leaving it as an implicit
property of "nobody else happens to read this env var".

Existing tests (``tests/unit/test_sensor_provider.py``,
``test_notification_provider.py``, ``test_knowledge_provider.py``) already
import directly from each submodule (e.g. ``from integrations.sensor_provider
import SyntheticSensorProvider``) — that direct-submodule-import style is
the established, idiomatic pattern in this codebase and remains fully
supported; nothing about the re-exports below changes it. This module
additionally re-exports the three factory functions here so a caller that
wants the uniform ``get_X_provider()`` shape across all three interfaces
(e.g. a future ``surface/entrypoint.py`` cold-start routine, task 17.1, that
constructs every provider in one place) can do
``from integrations import get_sensor_provider, get_knowledge_provider,
notification_provider`` without needing to know which submodule each
factory lives in. Both import styles resolve to the exact same function
objects; re-exporting adds a second way to reach them, it does not replace
the first.

Cold-start live-resource/credential-absent abort (Req 19.13, 19.5, 18.10)
---------------------------------------------------------------------------
Req 19.13: "IF a credential or a provisioned resource identifier required
by any live external interface (State_Store, Knowledge_Base,
Notification_Provider, the real-time push interface, the authentication
provider, or the agent runtime) is absent, THEN THE ThunAI_Platform SHALL
abort the run before the first call to that interface, SHALL report an
error naming each absent credential or resource identifier, and SHALL
leave State_Store unchanged."

:func:`check_required_live_resources` collects every problem rather than
raising on the first one (mirroring ``agents.config.validate_config()`` and
``policy.escalation_policy.validate_policy()``'s pattern exactly — see those
modules' docstrings), and :func:`abort_if_live_resources_missing` raises a
single :class:`MissingLiveResourceError` naming every absent value if the
check finds any.

The six identifiers checked, and why each one is included:

- ``THUNAI_KNOWLEDGE_BASE_ID`` (Knowledge_Provider, always live). No safe
  in-code default exists — ``knowledge_provider.py::get_knowledge_provider()``
  passes ``os.environ.get("THUNAI_KNOWLEDGE_BASE_ID", "")`` straight into
  ``BedrockKBKnowledgeProvider``, which itself raises
  ``KnowledgeProviderConfigurationError`` on an empty value. Checked here so
  the *first* thing a cold start reports is a clear, named list of gaps
  covering every live interface at once, rather than a caller discovering
  this one gap only when it happens to be the first provider constructed.
- ``THUNAI_SES_SENDER`` (Notification_Provider, always live). No safe
  in-code default — ``notification_provider.py``'s ``send_email`` raises
  ``NotificationSendError`` when unset. Checked for the same reason.
- ``THUNAI_COGNITO_USER_POOL_ID`` (the authentication provider, named
  explicitly by Req 18.10/19.13 and by ``.env.example``'s "Authentication"
  section). No auth-consuming module exists yet in this codebase (tasks
  21.3/23.1 are not yet implemented), but the check is included anyway per
  the task description, so that this shared cold-start check already
  covers that gap the moment a future auth-consuming module starts relying
  on it — there is no reason to wait for that module to exist before this
  check can name the pool id as missing.
- ``THUNAI_STATE_TABLE``, ``THUNAI_AUDIT_TABLE``, ``THUNAI_MEMORY_TABLE``
  (State_Store / Audit_Ledger / Memory_Store, always live).

  Reasoning on whether an unset env var here is a "problem" (documented per
  the task instructions, since ``memory/state_store.py`` and
  ``memory/audit_ledger.py`` already fall back to safe in-code defaults
  ``"thunai-state"``/``"thunai-audit"`` that match ``.env.example``'s
  committed non-blank default values — unlike
  ``THUNAI_KNOWLEDGE_BASE_ID``/``THUNAI_SES_SENDER``/
  ``THUNAI_COGNITO_USER_POOL_ID``, which ship as blank placeholders in
  ``.env.example`` with no working in-code fallback): Req 19.13's text does
  not carve out an exception for a "provisioned resource identifier" that
  happens to have a matching in-code default — it says to name *every*
  absent identifier "required by" a live interface, and a CDK-provisioned
  table name is exactly that kind of identifier in a real deployment (the
  Deployment_App is expected to inject the actual table name the Data stack
  created, per design.md §3.11; a silently-defaulted table name in a live
  deployment would mean the agent is reading/writing a table the deployer
  never actually verified is the right one). So this check treats "unset"
  as worth flagging for these three as well, consistent with
  ``validate_config``/``validate_policy``'s "collect every problem, even a
  non-fatal one, and name it" pattern — flagging it does not mean the
  process "will crash" without it (it will not; the in-code default still
  works), only that the identifier was not explicitly configured, which
  ``check_required_live_resources`` treats as a configuration gap worth
  surfacing per Req 19.13's own wording, exactly as the task's guidance
  describes.

Calling convention (Req 19.13's "leave State_Store unchanged")
------------------------------------------------------------------
:func:`abort_if_live_resources_missing` performs no reads or writes of its
own — it only inspects ``os.environ`` — so it trivially leaves State_Store
(and every other live resource) unchanged. The "before the first call to
that interface" half of Req 19.13 is a calling-convention contract this
function documents for its callers: a cold-start entrypoint (e.g.
``surface/entrypoint.py``'s ``@app.entrypoint`` handler, task 17.1, not yet
implemented) MUST call this function before constructing or invoking any
provider returned by ``get_sensor_provider()`` (when live),
``notification_provider()``, or ``get_knowledge_provider()`` — this module
cannot itself enforce that ordering across the codebase; it only reports
what the caller must act on.
"""

from __future__ import annotations

import os
from typing import Final

from integrations.knowledge_provider import get_knowledge_provider
from integrations.notification_provider import notification_provider
from integrations.sensor_provider import get_sensor_provider

__all__ = [
    "get_sensor_provider",
    "notification_provider",
    "get_knowledge_provider",
    "SYNTHETIC_SENSORS_AFFECTS",
    "MissingLiveResourceError",
    "check_required_live_resources",
    "abort_if_live_resources_missing",
]

# ---------------------------------------------------------------------------
# SYNTHETIC_SENSORS scope (Req 19.5): documents, as an importable constant,
# that this flag affects Sensor_Provider only. True by construction, since
# integrations/sensor_provider.py is the only module in this package that
# reads SYNTHETIC_SENSORS at all.
# ---------------------------------------------------------------------------

SYNTHETIC_SENSORS_AFFECTS: Final[frozenset[str]] = frozenset({"sensor_provider"})
"""The set of ``integrations/`` submodules whose backend selection is
affected by ``SYNTHETIC_SENSORS`` (Req 19.5). Every other integration
(``notification_provider``, ``knowledge_provider``) is live in every
environment and has no such flag."""


# ---------------------------------------------------------------------------
# Required live-resource identifiers (Req 19.13, 18.10). Each entry is
# (env_var_name, human-readable description of what it is required by).
# ---------------------------------------------------------------------------

_REQUIRED_LIVE_RESOURCE_ENV_VARS: Final[tuple[tuple[str, str], ...]] = (
    ("THUNAI_KNOWLEDGE_BASE_ID", "Knowledge_Provider (Bedrock Knowledge Base id)"),
    ("THUNAI_SES_SENDER", "Notification_Provider (verified SES sender identity)"),
    ("THUNAI_COGNITO_USER_POOL_ID", "the authentication provider (Cognito user pool id)"),
    ("THUNAI_STATE_TABLE", "State_Store (DynamoDB table name)"),
    ("THUNAI_AUDIT_TABLE", "Audit_Ledger (DynamoDB table name)"),
    ("THUNAI_MEMORY_TABLE", "Memory_Store (DynamoDB table name)"),
)


class MissingLiveResourceError(RuntimeError):
    """Raised by :func:`abort_if_live_resources_missing` when one or more
    required live-resource identifiers/credentials are absent from the
    environment (Req 19.13).

    Carries the full list of problem descriptions (one per absent value) as
    :attr:`problems`, in addition to being formatted into the exception
    message so a plain ``str(exc)`` already names every gap.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "abort_if_live_resources_missing found "
            f"{len(self.problems)} missing live-resource identifier(s): " + "; ".join(self.problems)
        )


def check_required_live_resources(env: dict[str, str] | None = None) -> list[str]:
    """Collect every absent required live-resource identifier/credential (Req 19.13).

    Mirrors ``agents.config.validate_config()``'s and
    ``policy.escalation_policy.validate_policy()``'s "collect every problem,
    don't raise on the first one" pattern: this function never raises, it
    only reports.

    Args:
        env: The environment mapping to check. Defaults to ``os.environ``.
            A caller (typically a test) may pass an alternate mapping to
            validate a hypothetical environment without mutating process
            state, matching ``validate_config``/``validate_policy``'s
            testable-override convention.

    Returns:
        A list of human-readable problem descriptions, each naming the
        specific missing environment variable and what it is required by
        (per Req 19.13's "naming each absent value"). Empty when every
        required identifier is set to a non-empty value.
    """
    source = env if env is not None else os.environ

    problems: list[str] = []
    for var_name, required_by in _REQUIRED_LIVE_RESOURCE_ENV_VARS:
        value = source.get(var_name, "")
        if not value:
            problems.append(f"{var_name} is not set (or is empty); required by {required_by}")
    return problems


def abort_if_live_resources_missing(env: dict[str, str] | None = None) -> None:
    """Abort with :class:`MissingLiveResourceError` if any required live resource is absent (Req 19.13).

    Calling convention: callers MUST invoke this function before
    constructing or invoking any provider returned by
    ``get_sensor_provider()`` (when live), ``notification_provider()``, or
    ``get_knowledge_provider()`` — i.e. before the first call to any of
    those live interfaces — per Req 19.13's "abort the run before the first
    call to that interface". This function performs no reads or writes of
    its own (it only inspects the environment mapping), so calling it
    trivially satisfies Req 19.13's "leave State_Store unchanged" as long as
    the caller has not already performed a write before calling it.

    Args:
        env: The environment mapping to check. Defaults to ``os.environ``;
            see :func:`check_required_live_resources` for the override use
            case.

    Raises:
        MissingLiveResourceError: Naming every absent required identifier,
            if any. Raises nothing (returns ``None``) when every check
            passes.
    """
    problems = check_required_live_resources(env)
    if problems:
        raise MissingLiveResourceError(problems)
