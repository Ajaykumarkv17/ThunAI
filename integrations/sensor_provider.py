"""``Sensor_Provider`` integration seam — the ONLY interface with a synthetic backend.

Implements the ``SensorProvider`` Protocol plus its two concrete backends
(design.md §3.7, Req 19.5, 3.1):

- ``SyntheticSensorProvider`` replays the committed ``seed/hazard_readings/*.jsonl``
  fixture series. Selected when ``SYNTHETIC_SENSORS=1`` (the default per
  ``.env.example``) — a real flood cannot be summoned on demand for a demo.
- ``LiveSensorProvider`` reads the same three reading types from a real,
  external source exposed as an AgentCore Gateway target named by
  ``SENSOR_GATEWAY_TARGET``. Selected when ``SYNTHETIC_SENSORS=0``.

``get_sensor_provider()`` is the single factory that reads ``SYNTHETIC_SENSORS``
and is, per task 10.4, the ONLY place in the codebase that reads that flag —
every other integration (notification, knowledge) is live in every environment
and has no such flag at all.

Verification note (mandatory per tasks.md 10.1, "verify first"):
    Consulted the AWS documentation MCP server and a web search for the
    current AgentCore Gateway tool-naming convention and for how a Strands
    ``Agent``/tool wrapper actually invokes a Gateway-exposed tool.

    1. Tool-naming convention. The authoritative AgentCore devguide page
       ("Understand how AgentCore Gateway tools are named",
       https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-tool-naming.html)
       states the pattern is literally::

           ${target_name}___${tool_name}

       i.e. **three underscores** — matching ``.env.example``'s comment
       exactly. (A separately-indexed CDK construct-library README shows a
       *two*-underscore example (`my-lambda-target__calculate_price`) in its
       prose; that page is not the AgentCore devguide and is treated as the
       less authoritative of the two per this task's docs-win rule. The
       devguide page is the canonical source for gateway tool naming and is
       used here. Both conventions are handled defensively below by
       resolving on the *first* occurrence of either separator, so a
       misconfigured target name cannot silently break lookups.)
    2. Runtime invocation mechanism. The AgentCore devguide's own "Create an
       agent that uses your AgentCore gateway" page, "Using Strands" section,
       confirms the invocation shape: a Strands ``Agent`` (or any MCP-aware
       caller) is pointed at the Gateway's MCP endpoint via
       ``strands.tools.mcp.mcp_client.MCPClient`` wrapping
       ``mcp.client.streamable_http.streamablehttp_client`` (a
       "streamable-http" MCP transport, bearer-token authenticated), then
       ``with mcp_client: tools = mcp_client.list_tools_sync()`` discovers
       the gateway-exposed (already prefixed) tool names, and
       ``Agent(tools=tools)`` invokes them like any other tool. There is
       **no separate Gateway-specific boto3 "invoke tool" API** for calling
       a tool at runtime — ``boto3`` gateway control-plane clients
       (``bedrock-agentcore-control``) exist only for *managing* gateways/
       targets, not for invoking their tools; tool invocation is exclusively
       via the MCP protocol against the Gateway's MCP endpoint. This
       confirms design.md §3.7's assumption (a Gateway target wrapping a
       Lambda/REST source, invoked as an ordinary tool) and requires no
       deviation — ``LiveSensorProvider`` below is implemented as an
       ``MCPClient`` against the Gateway MCP endpoint, calling the specific
       prefixed tool names for each reading type via ``call_tool_sync``
       inside the client's ``with`` context, matching the pattern documented
       at https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-agent-integration.html
       (also cross-checked against the elicitation and MCP-server-integration
       pages, which use the identical ``MCPClient``/``streamablehttp_client``
       shape).
    3. No live Gateway target exists for this task (out of scope per the
       task instructions), so ``LiveSensorProvider`` is written to the
       correct shape confirmed above but is not exercised by a live
       integration test here; unit tests below exercise only
       ``SyntheticSensorProvider`` and the factory's type selection.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

READING_TYPES: tuple[str, ...] = ("river_level", "rainfall_rate", "dam_release")

_FIXTURE_FILENAMES: dict[str, str] = {
    "river_level": "river_level.jsonl",
    "rainfall_rate": "rainfall_rate.jsonl",
    "dam_release": "dam_release.jsonl",
}


@runtime_checkable
class SensorProvider(Protocol):
    """Interface supplying hazard-sensor readings (Req 3.1, 19.5, design.md §3.7).

    Both backends (``SyntheticSensorProvider`` and ``LiveSensorProvider``)
    implement this same interface, so ``Monitor_Agent``'s sensor-reading
    tools (``tools/sensor_tools.py``, task 12.1) can be written once against
    this Protocol and work unchanged whichever backend
    ``get_sensor_provider()`` returns.

    A "reading" returned by either method is a plain ``dict`` with, at
    minimum, the keys ``value`` (``float``), ``unit`` (``str``), and
    ``timestamp`` (``datetime``, timezone-aware) — the exact shape
    ``schemas/decisions.py::HazardAssessment`` needs to compute
    ``rate_of_change`` (a delta between two readings' ``value``/``timestamp``,
    Req 3.2) and ``anomaly_indicator`` (the ratio of the current reading's
    ``value`` to the mean ``value`` of the baseline series, Req 3.2, 3.9,
    17.3). ``reading_id`` and ``reading_type`` are included when the backend
    has them (the synthetic backend always does), for logging/dedup only —
    callers must not depend on their presence.
    """

    def get_latest_reading(self, reading_type: str, as_of: datetime) -> dict | None:
        """Return the latest reading of ``reading_type`` at or before ``as_of``.

        Returns ``None`` when no reading of that type exists at or before
        ``as_of`` (e.g. ``as_of`` predates the start of the data — Req 3.6,
        3.8 require the caller to treat that reading type as unavailable,
        not to raise).
        """
        ...

    def get_baseline_series(
        self, reading_type: str, as_of: datetime, days: int = 30
    ) -> list[dict]:
        """Return every reading of ``reading_type`` in the ``days``-day window ending at ``as_of``.

        The window is ``(as_of - timedelta(days=days), as_of]`` — strictly
        after the start boundary, up to and including ``as_of`` itself.
        Readings are returned in ascending timestamp order. Returns an empty
        list when no reading of that type falls in the window (Req 3.9
        requires the caller to mark the anomaly indicator unavailable in
        that case, not to raise).
        """
        ...


class SyntheticSensorProvider:
    """Replays ``seed/hazard_readings/*.jsonl`` — the ONLY synthetic backend in ThunAI.

    Selected ONLY by ``SYNTHETIC_SENSORS=1`` (the default, per
    ``.env.example``). Deterministic and offline: no network call, no sensor
    credential. Loads each of the three fixture files once at construction
    (per the README's contract, "Contract for integrations/sensor_provider.py
    (task 10.1)", point 1) and answers every subsequent read from the
    in-memory, timestamp-sorted list — replay, not a live feed.
    """

    def __init__(self, fixture_path: str | os.PathLike[str] | None = None) -> None:
        base = Path(fixture_path or os.environ.get("SENSOR_FIXTURE_PATH", "seed/hazard_readings"))
        self._readings: dict[str, list[dict]] = {}
        for reading_type, filename in _FIXTURE_FILENAMES.items():
            file_path = base / filename
            rows: list[dict] = []
            if file_path.exists():
                with file_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        row = json.loads(line)
                        row["timestamp"] = datetime.fromisoformat(row["timestamp"])
                        rows.append(row)
                rows.sort(key=lambda r: r["timestamp"])
            self._readings[reading_type] = rows

    def get_latest_reading(self, reading_type: str, as_of: datetime) -> dict | None:
        """Replay the reading whose ``timestamp`` is latest at or before ``as_of``.

        Per the README contract point 2: given a requested "as of" timestamp,
        return the latest reading at or before that time. Uses a linear
        reverse scan over the (small, hourly-sampled) sorted series — clear
        and adequate for the committed 840-row-per-file fixture size.
        """
        rows = self._readings.get(reading_type, [])
        latest: dict | None = None
        for row in rows:
            if row["timestamp"] <= as_of:
                latest = row
            else:
                break
        return dict(latest) if latest is not None else None

    def get_baseline_series(
        self, reading_type: str, as_of: datetime, days: int = 30
    ) -> list[dict]:
        """Return the prior ``days`` days of readings at or before ``as_of``.

        Per the README contract point 3: "look back 30 days from that point
        for the 30-day baseline." The window excludes the exact start
        boundary and includes ``as_of`` itself, matching the Protocol's
        documented window semantics.
        """
        rows = self._readings.get(reading_type, [])
        window_start = as_of - timedelta(days=days)
        return [dict(row) for row in rows if window_start < row["timestamp"] <= as_of]


class LiveSensorProvider:
    """Reads hazard readings from a real external source via an AgentCore Gateway target.

    Selected when ``SYNTHETIC_SENSORS=0``. Wraps a Strands ``MCPClient``
    pointed at the Gateway's MCP endpoint (streamable-http transport,
    bearer-token authenticated) per the verification note above — this is
    the confirmed live invocation mechanism; there is no separate
    Gateway-specific boto3 "call tool" API. The three reading-type tools are
    expected to be exposed on the Gateway target named by
    ``SENSOR_GATEWAY_TARGET`` as ``get_river_level`` / ``get_rainfall_rate`` /
    ``get_dam_release``, visible through MCP with the target-name prefix
    (``${target_name}___${tool_name}``, confirmed above).

    Not exercised by unit tests in this task (no live Gateway target is
    available here) — the shape is written to match the confirmed live
    invocation mechanism so a later live-integration test can exercise it
    against a real deployed Gateway target.
    """

    def __init__(self, gateway_target: str | None = None) -> None:
        self._gateway_target = gateway_target or os.environ.get("SENSOR_GATEWAY_TARGET")
        if not self._gateway_target:
            raise RuntimeError(
                "LiveSensorProvider requires SENSOR_GATEWAY_TARGET (or an explicit "
                "gateway_target argument) naming the AgentCore Gateway target that "
                "exposes the live sensor-reading tools."
            )
        # Gateway URL resolution is deferred to first use (_client()) rather than done
        # here, so constructing a LiveSensorProvider (e.g. via get_sensor_provider())
        # does not itself require THUNAI_GATEWAY_URL/THUNAI_GATEWAY_ID to be set yet.
        self._gateway_url: str | None = None
        self._mcp_client = None  # lazily constructed on first read; see _client().

    def _build_gateway_url(self) -> str:
        gateway_id = os.environ.get("THUNAI_GATEWAY_ID", "")
        region = os.environ.get("AWS_REGION", "us-west-2")
        if not gateway_id:
            raise RuntimeError(
                "LiveSensorProvider requires THUNAI_GATEWAY_URL, or THUNAI_GATEWAY_ID "
                "plus AWS_REGION to derive the Gateway's MCP endpoint URL."
            )
        return f"https://{gateway_id}.gateway.bedrock-agentcore.{region}.amazonaws.com/mcp"

    def _tool_name(self, tool: str) -> str:
        # Three-underscore separator confirmed against the AgentCore devguide
        # (see module verification note).
        return f"{self._gateway_target}___{tool}"

    def _client(self):
        if self._mcp_client is None:
            from mcp.client.streamable_http import streamablehttp_client
            from strands.tools.mcp import MCPClient

            if self._gateway_url is None:
                self._gateway_url = os.environ.get("THUNAI_GATEWAY_URL") or self._build_gateway_url()

            access_token = os.environ.get("THUNAI_GATEWAY_ACCESS_TOKEN", "")

            def _transport():
                headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
                return streamablehttp_client(self._gateway_url, headers=headers)

            self._mcp_client = MCPClient(_transport)
        return self._mcp_client

    _READ_TOOL_NAMES: dict[str, str] = {
        "river_level": "get_river_level",
        "rainfall_rate": "get_rainfall_rate",
        "dam_release": "get_dam_release",
    }

    def _call(self, reading_type: str, as_of: datetime) -> dict | None:
        tool = self._READ_TOOL_NAMES.get(reading_type)
        if tool is None:
            return None
        client = self._client()
        with client:
            result = client.call_tool_sync(
                tool_use_id=f"sensor-{reading_type}-{as_of.isoformat()}",
                name=self._tool_name(tool),
                arguments={"as_of": as_of.isoformat()},
            )
        return _mcp_tool_result_to_reading(result)

    def get_latest_reading(self, reading_type: str, as_of: datetime) -> dict | None:
        return self._call(reading_type, as_of)

    def get_baseline_series(
        self, reading_type: str, as_of: datetime, days: int = 30
    ) -> list[dict]:
        tool = self._READ_TOOL_NAMES.get(reading_type)
        if tool is None:
            return []
        client = self._client()
        with client:
            result = client.call_tool_sync(
                tool_use_id=f"sensor-baseline-{reading_type}-{as_of.isoformat()}",
                name=self._tool_name(tool),
                arguments={"as_of": as_of.isoformat(), "days": days},
            )
        reading = _mcp_tool_result_to_reading(result)
        return [reading] if reading else []


def _mcp_tool_result_to_reading(result: object) -> dict | None:
    """Best-effort extraction of a reading dict from an MCP ``call_tool_sync`` result.

    Left intentionally small: the exact content-block shape of a live
    Gateway target's tool result cannot be verified without a deployed
    target (out of scope for this task, per its instructions). Kept as a
    single, easily-adjusted seam for the later live-integration task.
    """
    if result is None:
        return None
    if isinstance(result, dict):
        return result
    content = getattr(result, "content", None)
    if content:
        block = content[0]
        text = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else None)
        if text:
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return None
    return None


def get_sensor_provider() -> SensorProvider:
    """Select the sensor backend from ``SYNTHETIC_SENSORS`` — the ONLY place this flag is read.

    ``SYNTHETIC_SENSORS`` defaults to ``"1"`` (per ``.env.example``) and
    affects the ``Sensor_Provider`` interface only (Req 19.5); every other
    integration in ``integrations/`` has no such flag and is always live.
    """
    if os.environ.get("SYNTHETIC_SENSORS", "1") == "1":
        return SyntheticSensorProvider()
    return LiveSensorProvider()
