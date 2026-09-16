import asyncio
import json
import time
from typing import Any, Protocol

import httpx

from aegisops.config import Settings
from aegisops.domain import Query, RemoteFailure, ToolOutput

KINDS = {
    "query_metrics": "metrics",
    "query_logs": "logs",
    "query_traces": "traces",
    "service_topology": "topology",
    "get_deploy_metadata": "deploy",
}


async def bounded_json(
    client: httpx.AsyncClient, method: str, url: str, cap: int, **kwargs: Any
) -> Any:
    try:
        async with client.stream(method, url, **kwargs) as response:
            if response.status_code == 429:
                raise RemoteFailure("http_429")
            if response.status_code >= 500:
                raise RemoteFailure("http_5xx")
            if response.status_code >= 300:
                raise RemoteFailure("http_rejected", retryable=False)
            parts = bytearray()
            async for part in response.aiter_bytes():
                parts.extend(part)
                if len(parts) > cap:
                    raise RemoteFailure("response_too_large", retryable=False)
            try:
                return json.loads(parts)
            except (ValueError, UnicodeError) as exc:
                raise RemoteFailure("malformed_json") from exc
    except httpx.TimeoutException as exc:
        raise RemoteFailure("timeout") from exc
    except httpx.NetworkError as exc:
        raise RemoteFailure("network_error") from exc


class DataSource(Protocol):
    async def read(self, name: str, args: Query, ordinal: int) -> ToolOutput: ...


class ReplayDataSource:
    def __init__(self, settings: Settings, scenario: str) -> None:
        # The scenario is validated against filenames, never accepted as a path.
        available = {p.stem: p for p in settings.fixtures_dir.glob("*.json")}
        if scenario not in available:
            raise ValueError("unknown_scenario")
        raw = json.loads(available[scenario].read_text(encoding="utf-8"))
        self.telemetry = raw["telemetry"]
        self.failures = raw.get("failures", {})

    async def read(self, name: str, args: Query, ordinal: int) -> ToolOutput:
        failures = self.failures.get(name, [])
        outcome = failures[ordinal] if ordinal < len(failures) else "ok"
        if outcome == "timeout":
            # Yield to exercise cancellation; timeout classification is deterministic.
            await asyncio.sleep(0)
            raise RemoteFailure("timeout")
        if outcome in {"http_429", "http_5xx", "malformed_json"}:
            raise RemoteFailure(outcome)
        records = self.telemetry[KINDS[name]]
        filtered = [
            r for r in records if name == "service_topology" or r.get("service") == args.service
        ]
        if outcome == "empty":
            filtered = []
        selected = filtered[args.cursor : args.cursor + args.limit]
        next_cursor = (
            args.cursor + len(selected) if len(filtered) > args.cursor + len(selected) else None
        )
        return ToolOutput(
            source_type=KINDS[name],
            source_name="replay:" + KINDS[name],
            records=selected,
            next_cursor=next_cursor,
        )


class LiveDataSource:
    def __init__(self, settings: Settings) -> None:
        self.cfg = settings

    async def read(self, name: str, args: Query, ordinal: int) -> ToolOutput:
        c = self.cfg
        records: list[dict[str, Any]] = []
        timeout = httpx.Timeout(c.io_timeout, connect=min(3, c.io_timeout))
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, trust_env=False
        ) as client:
            if name == "service_topology":
                records = [{"service": "order-service", "depends_on": "inventory-service"}]
            elif name == "get_deploy_metadata":
                endpoint = (
                    c.demo_order_url if args.service == "order-service" else c.demo_inventory_url
                )
                result = await bounded_json(client, "GET", endpoint + "/metadata", c.max_http_bytes)
                records = [result]
            elif name == "query_metrics":
                record: dict[str, Any] = {"service": args.service}
                for metric, key, aggregation in [
                    ("aegis_demo_error_ratio", "error_rate", "avg_over_time"),
                    ("aegis_demo_latency_ms", "latency_ms", "max_over_time"),
                    ("aegis_demo_rate_limited", "rate_limited", "max_over_time"),
                ]:
                    query = f'{aggregation}({metric}{{service="{args.service}"}}[{args.window_seconds}s])'
                    data = await bounded_json(
                        client,
                        "GET",
                        c.prometheus_url + "/api/v1/query",
                        c.max_http_bytes,
                        params={"query": query},
                    )
                    points = data.get("data", {}).get("result", [])
                    if points:
                        record[key] = float(points[0]["value"][1])
                if len(record) > 1:
                    records = [record]
            elif name == "query_logs":
                end = time.time_ns()
                data = await bounded_json(
                    client,
                    "GET",
                    c.loki_url + "/loki/api/v1/query_range",
                    c.max_http_bytes,
                    params={
                        "query": '{service="' + args.service + '"}',
                        "start": end - args.window_seconds * 10**9,
                        "end": end,
                        "limit": args.limit + args.cursor,
                        "direction": "backward",
                    },
                )
                for stream in data.get("data", {}).get("result", []):
                    for ts, line in stream.get("values", []):
                        records.append({"service": args.service, "timestamp": ts, "message": line})
            elif name == "query_traces":
                end_seconds = int(time.time())
                data = await bounded_json(
                    client,
                    "GET",
                    c.tempo_url + "/api/search",
                    c.max_http_bytes,
                    params={
                        "q": '{ resource.service.name = "' + args.service + '" }',
                        "start": end_seconds - args.window_seconds,
                        "end": end_seconds,
                        "limit": min(args.limit, 5),
                    },
                )
                for trace in data.get("traces", [])[:5]:
                    trace_id = trace.get("traceID", "")
                    if len(trace_id) != 32 or any(
                        ch not in "0123456789abcdef" for ch in trace_id.lower()
                    ):
                        continue
                    body = await bounded_json(
                        client,
                        "GET",
                        c.tempo_url + "/api/traces/" + trace_id,
                        c.max_http_bytes,
                        headers={"Accept": "application/json"},
                    )
                    for batch in body.get("batches", body.get("resourceSpans", [])):
                        resource = {
                            a["key"]: a.get("value", {}).get("stringValue")
                            for a in batch.get("resource", {}).get("attributes", [])
                        }
                        for group in batch.get(
                            "scopeSpans", batch.get("instrumentationLibrarySpans", [])
                        ):
                            for item in group.get("spans", []):
                                attrs = {
                                    a["key"]: a.get("value", {}).get("stringValue")
                                    for a in item.get("attributes", [])
                                }
                                records.append(
                                    {
                                        "service": resource.get("service.name"),
                                        "trace_id": trace_id,
                                        "span": item.get("name"),
                                        "dependency": attrs.get("peer.service"),
                                        "error": item.get("status", {}).get("code")
                                        in (2, "STATUS_CODE_ERROR"),
                                        "duration_ms": (
                                            int(item.get("endTimeUnixNano", 0))
                                            - int(item.get("startTimeUnixNano", 0))
                                        )
                                        / 1e6,
                                    }
                                )
        page = records[args.cursor : args.cursor + args.limit]
        return ToolOutput(
            source_type=KINDS[name],
            source_name="live:" + KINDS[name],
            records=page,
            next_cursor=args.cursor + len(page) if len(records) > args.cursor + len(page) else None,
        )
