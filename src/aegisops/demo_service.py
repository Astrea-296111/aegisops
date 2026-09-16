import asyncio
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, StatusCode
from prometheus_client import CONTENT_TYPE_LATEST, Gauge, generate_latest
from sqlalchemy import select, update

from aegisops.config import Settings
from aegisops.domain import StrictModel
from aegisops.persistence.database import Database, db_now
from aegisops.persistence.models import DemoResource
from aegisops.telemetry.instrumentation import configure

LATENCY = Gauge("aegis_demo_latency_ms", "Last demo business request latency", ["service"])
ERROR = Gauge("aegis_demo_error_ratio", "Whether the last demo request failed", ["service"])
LIMITED = Gauge("aegis_demo_rate_limited", "Whether last request received 429", ["service"])
LOG = logging.getLogger("aegisops.demo")


class Fault(StrictModel):
    fault: Literal["none", "latency", "error", "overload"] = "none"
    version: Literal["v1", "v2"] = "v1"


def create_demo_app(cfg: Settings, service: str) -> FastAPI:
    if service not in ("order-service", "inventory-service"):
        raise ValueError("unknown_service")
    if not cfg.demo_only or len(cfg.demo_token.get_secret_value()) < 24:
        raise RuntimeError("DEMO_ONLY: enable AEGIS_DEMO_ONLY and configure a random demo token")
    db = Database(cfg.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        configure(cfg.otlp_endpoint, service)
        yield
        await db.close()

    app = FastAPI(title="DEMO_ONLY: " + service, lifespan=lifespan)

    async def resource() -> DemoResource:
        async with db.sessions() as s:
            item = await s.get(DemoResource, service)
            if item is None:
                raise HTTPException(503, "run migrations first")
            return item

    @app.get("/health")
    async def health() -> dict[str, str]:
        await resource()
        return {"status": "ok", "service": service}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/metadata")
    async def metadata() -> dict[str, Any]:
        item = await resource()
        return {
            "service": service,
            "version": item.version,
            "revision": item.revision,
            "deployed_at": item.updated_at,
        }

    @app.post("/demo/fault")
    async def fault(value: Fault, authorization: Annotated[str, Header()] = "") -> dict[str, Any]:
        if not hmac.compare_digest(authorization, "Bearer " + cfg.demo_token.get_secret_value()):
            raise HTTPException(403, "demo token required")
        async with db.sessions.begin() as s:
            await s.execute(
                update(DemoResource)
                .where(DemoResource.service == service)
                .values(service=DemoResource.service)
            )
            item = (
                await s.execute(select(DemoResource).where(DemoResource.service == service))
            ).scalar_one()
            item.fault, item.version, item.revision, item.updated_at = (
                value.fault,
                value.version,
                item.revision + 1,
                await db_now(s),
            )
        return {"DEMO_ONLY": True, "service": service, "revision": item.revision}

    @app.get("/work")
    async def work(request: Request) -> Response:
        started = time.perf_counter()
        item = await resource()
        status = 200
        tracer = trace.get_tracer("aegisops.demo")
        with tracer.start_as_current_span(
            "work",
            context=propagate.extract(dict(request.headers)),
            kind=SpanKind.SERVER,
            attributes={"service.version": item.version},
        ) as current:
            if item.fault == "latency":
                await asyncio.sleep(1.2)
            if item.fault == "error" or item.version == "v2":
                status = 500
            elif item.fault == "overload":
                status = 429
            elif service == "order-service":
                with tracer.start_as_current_span(
                    "inventory.reserve",
                    kind=SpanKind.CLIENT,
                    attributes={"peer.service": "inventory-service"},
                ) as downstream:
                    headers: dict[str, str] = {}
                    propagate.inject(headers)
                    try:
                        async with httpx.AsyncClient(
                            timeout=httpx.Timeout(2, connect=1), trust_env=False
                        ) as client:
                            response = await client.get(
                                cfg.demo_inventory_url + "/work", headers=headers
                            )
                        if response.status_code != 200:
                            status = 502
                            downstream.set_status(StatusCode.ERROR)
                    except httpx.HTTPError:
                        status = 504
                        downstream.set_status(StatusCode.ERROR)
            if status >= 400:
                current.set_status(StatusCode.ERROR)
            duration = (time.perf_counter() - started) * 1000
            trace_id = format(current.get_span_context().trace_id, "032x")
        LATENCY.labels(service).set(duration)
        ERROR.labels(service).set(int(status >= 400))
        LIMITED.labels(service).set(int(status == 429))
        log = json.dumps(
            {
                "service": service,
                "status": status,
                "duration_ms": round(duration, 2),
                "version": item.version,
                "trace_id": trace_id,
            }
        )
        LOG.info(log)
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                response = await client.post(
                    cfg.loki_url + "/loki/api/v1/push",
                    json={
                        "streams": [
                            {"stream": {"service": service}, "values": [[str(time.time_ns()), log]]}
                        ]
                    },
                )
                if response.status_code >= 300:
                    LOG.warning('{"event":"demo_log_export_failed"}')
        except httpx.HTTPError:
            LOG.warning('{"event":"demo_log_export_unavailable"}')
        return JSONResponse({"service": service, "status": status}, status_code=status)

    return app
