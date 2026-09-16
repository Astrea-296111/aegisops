import asyncio
import time
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select

from aegisops.config import Settings
from aegisops.domain import (
    BudgetExceeded,
    EvidenceView,
    PolicyDenied,
    RemoteFailure,
    ToolOutput,
    ToolRequest,
)
from aegisops.persistence.models import Evidence, ToolCall, new_id
from aegisops.persistence.repository import Lease, Repository, audit
from aegisops.security.guards import canonical, digest, injection_flag, redact
from aegisops.telemetry.instrumentation import COUNTER, span
from aegisops.tools.sources import KINDS, DataSource


def trim(value: Any) -> Any:
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, list):
        return [trim(v) for v in value[:50]]
    if isinstance(value, dict):
        return {k: trim(v) for k, v in list(value.items())[:30]}
    return value


def compact(output: ToolOutput, cap: int) -> tuple[ToolOutput, int, int, bool, bool]:
    raw = output.model_dump()
    flag = injection_flag(raw)
    original_size = len(canonical(raw).encode())
    clean = redact(raw)
    clean["records"] = trim(clean["records"])
    while clean["records"] and len(canonical(clean).encode()) > cap:
        clean["records"].pop()
    result = ToolOutput.model_validate(clean)
    size = len(canonical(clean).encode())
    return result, original_size, size, flag, clean != raw


class ToolRegistry:
    """All network reads and their durable budgets pass through this boundary."""

    def __init__(self, repo: Repository, source: DataSource, settings: Settings) -> None:
        self.repo, self.source, self.cfg = repo, source, settings

    @staticmethod
    def schema() -> dict[str, Any]:
        return {
            "request": ToolRequest.model_json_schema(),
            "output": ToolOutput.model_json_schema(),
            "tools": [{"name": n, "risk": "READ_ONLY"} for n in KINDS],
        }

    async def execute(
        self, lease: Lease, step_id: str, request: ToolRequest, phase: str
    ) -> EvidenceView:
        request = ToolRequest.model_validate(request.model_dump())
        key = digest({"phase": phase, "request": request.model_dump()})
        async with self.repo.db.sessions() as s:
            existing = (
                await s.execute(
                    select(Evidence).where(
                        Evidence.run_id == lease.run_id, Evidence.call_key == key
                    )
                )
            ).scalar_one_or_none()
            if existing:
                return EvidenceView.model_validate(existing.data)
        async with self.repo.locked(lease.run_id, lease) as (s, run, now):
            if run.cancel_requested:
                raise PolicyDenied("cancellation_requested")
            if (
                run.tool_calls >= self.cfg.max_tool_calls
                or now - run.created_at > self.cfg.max_wall_seconds
            ):
                raise BudgetExceeded("tool_or_wall_budget")
            previous = int(
                (
                    await s.scalar(
                        select(func.count())
                        .select_from(ToolCall)
                        .where(ToolCall.run_id == run.id, ToolCall.call_key == key)
                    )
                )
                or 0
            )
            if previous >= self.cfg.max_attempts:
                raise RemoteFailure("tool_retry_exhausted", retryable=False)
            ordinal = int(
                (
                    await s.scalar(
                        select(func.count())
                        .select_from(ToolCall)
                        .where(ToolCall.run_id == run.id, ToolCall.tool_name == request.name)
                    )
                )
                or 0
            )
            call = ToolCall(
                id=new_id(),
                run_id=run.id,
                step_id=step_id,
                call_key=key,
                tool_name=request.name,
                args_json=request.args.model_dump(),
                retry_count=previous,
            )
            s.add(call)
            run.tool_calls += 1
            audit(
                s,
                run,
                lease.owner,
                "tool.started",
                {"tool": request.name, "call_id": call.id, "retry_count": previous},
                now,
            )
            call_id = call.id
        started = time.perf_counter()
        try:
            with span(
                "tool.call",
                **{"run.id": lease.run_id, "tool.name": request.name, "retry.count": previous},
            ):
                async with asyncio.timeout(self.cfg.io_timeout):
                    output = await self.source.read(request.name, request.args, ordinal)
                    output = ToolOutput.model_validate(output.model_dump())
                clean, original_size, size, flag, truncated = compact(
                    output, self.cfg.max_tool_bytes
                )
        except (
            RemoteFailure,
            TimeoutError,
            ValidationError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            failure = (
                exc
                if isinstance(exc, RemoteFailure)
                else RemoteFailure(
                    "timeout" if isinstance(exc, TimeoutError) else "malformed_response"
                )
            )
            async with self.repo.locked(lease.run_id, lease) as (s, run, now):
                stored = await s.get(ToolCall, call_id)
                assert stored is not None
                stored.status, stored.error_type, stored.finished_at = (
                    "failed",
                    failure.category,
                    now,
                )
                stored.latency_ms = (time.perf_counter() - started) * 1000
                audit(
                    s,
                    run,
                    lease.owner,
                    "tool.failed",
                    {"call_id": call_id, "category": failure.category},
                    now,
                )
            COUNTER.labels("tool", "failed").inc()
            raise failure from exc
        async with self.repo.locked(lease.run_id, lease) as (s, run, now):
            ident = new_id()
            evidence = EvidenceView(
                id=ident,
                source_type=clean.source_type,
                source_name=clean.source_name,
                query={
                    **request.args.model_dump(),
                    "tool": request.name,
                    "phase": phase,
                    "next_cursor": clean.next_cursor,
                    "window_end": now,
                    "window_start": now - request.args.window_seconds,
                },
                observed_at=now,
                summary=canonical(clean.records[:1])[:400],
                content_hash=digest(clean.model_dump()),
                payload_excerpt=clean.records,
                injection_flag=flag,
                truncated=truncated,
            )
            s.add(Evidence(id=ident, run_id=run.id, call_key=key, data=evidence.model_dump()))
            stored = await s.get(ToolCall, call_id)
            assert stored is not None
            stored.status, stored.finished_at, stored.output_hash, stored.output_size = (
                "succeeded",
                now,
                evidence.content_hash,
                original_size,
            )
            stored.latency_ms = (time.perf_counter() - started) * 1000
            run.output_bytes += original_size
            run.compacted_bytes += size
            audit(
                s,
                run,
                lease.owner,
                "tool.succeeded",
                {"call_id": call_id, "evidence_id": ident, "injection_flag": flag, "bytes": size},
                now,
            )
        COUNTER.labels("tool", "succeeded").inc()
        return evidence
