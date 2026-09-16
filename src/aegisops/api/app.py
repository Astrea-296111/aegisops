import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select, text

from aegisops.config import Settings
from aegisops.domain import DecisionInput, IncidentInput, PolicyDenied
from aegisops.persistence.database import Database
from aegisops.persistence.models import Approval, Execution, Run, ToolCall
from aegisops.persistence.repository import Repository, row_dict
from aegisops.security.guards import verify_chain
from aegisops.telemetry.instrumentation import configure, span
from aegisops.tools.registry import ToolRegistry

bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None, database: Database | None = None) -> FastAPI:
    cfg = settings or Settings()
    db = database or Database(cfg.database_url)
    repo = Repository(db, cfg)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        tokens = (cfg.viewer_token.get_secret_value(), cfg.operator_token.get_secret_value())
        if any(len(t) < 24 for t in tokens) or tokens[0] == tokens[1]:
            raise RuntimeError(
                "Run 'aegisops init' first; use two distinct random API tokens (24+ chars)."
            )
        configure(cfg.otlp_endpoint)
        yield
        if database is None:
            await db.close()

    app = FastAPI(
        title="AegisOps",
        version="0.1.0",
        lifespan=lifespan,
        description="Evidence-grounded demo incident investigations. Authorize with the token from your local .env.",
    )
    app.state.repo = repo

    async def role(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if credentials is not None:
            for label, secret in [("operator", cfg.operator_token), ("viewer", cfg.viewer_token)]:
                expected = secret.get_secret_value()
                if expected and hmac.compare_digest(credentials.credentials, expected):
                    return label
        raise HTTPException(401, "invalid bearer token", headers={"WWW-Authenticate": "Bearer"})

    Auth = Annotated[str, Depends(role)]

    @app.exception_handler(KeyError)
    async def missing(_request: Request, _exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "resource not found"})

    @app.exception_handler(PolicyDenied)
    async def denied(_request: Request, exc: PolicyDenied) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> Response:
        try:
            async with db.sessions() as s:
                await s.execute(text("SELECT id FROM runs LIMIT 1"))
            return JSONResponse({"status": "ready"})
        except Exception:
            return JSONResponse({"status": "database_or_migration_unavailable"}, status_code=503)

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> Response:
        # SQL-derived totals include workers in other processes; in-process counters do not.
        async with db.sessions() as s:
            states = (await s.execute(select(Run.state, func.count()).group_by(Run.state))).all()
            calls = (
                await s.execute(select(ToolCall.status, func.count()).group_by(ToolCall.status))
            ).all()
            llm = (await s.scalar(select(func.sum(Run.llm_calls)))) or 0
            approval_count = (await s.scalar(select(func.count()).select_from(Approval))) or 0
            remediation_count = (await s.scalar(select(func.count()).select_from(Execution))) or 0
        lines = [
            "# TYPE aegis_runs_total counter",
            f"aegis_runs_total {sum(count for _, count in states)}",
            "# TYPE aegis_runs gauge",
        ]
        lines.extend(f'aegis_runs{{state="{state}"}} {count}' for state, count in states)
        lines.append(f"aegis_llm_calls_total {llm}")
        lines.append(
            f"aegis_runs_failed_total {sum(count for state, count in states if state in {'FAILED', 'PARTIAL'})}"
        )
        lines.append(
            f"aegis_tool_failures_total {sum(count for status, count in calls if status == 'failed')}"
        )
        lines.append(f"aegis_approval_total {approval_count}")
        lines.append(f"aegis_remediation_total {remediation_count}")
        lines.extend(
            f'aegis_tool_calls_total{{status="{status}"}} {count}' for status, count in calls
        )
        return Response(
            generate_latest() + ("\n".join(lines) + "\n").encode(), media_type=CONTENT_TYPE_LATEST
        )

    @app.get("/tools")
    async def tools(_role: Auth) -> dict[str, Any]:
        return ToolRegistry.schema()

    @app.post("/incidents", status_code=201)
    async def create_incident(value: IncidentInput, _role: Auth) -> dict[str, Any]:
        if cfg.source == "replay" and value.scenario not in {
            p.stem for p in cfg.fixtures_dir.glob("*.json")
        }:
            raise HTTPException(422, "unknown replay scenario")
        with span("incident.request", service=value.service):
            return row_dict(await repo.create_incident(value))

    @app.get("/incidents/{incident_id}")
    async def get_incident(incident_id: str, _role: Auth) -> dict[str, Any]:
        return row_dict(await repo.get_incident(incident_id))

    @app.post("/incidents/{incident_id}/investigate", status_code=202)
    async def investigate(incident_id: str, _role: Auth) -> dict[str, Any]:
        return row_dict(await repo.start(incident_id))

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str, _role: Auth) -> dict[str, Any]:
        return row_dict(await repo.get_run(run_id))

    @app.get("/runs/{run_id}/events")
    async def events(run_id: str, _role: Auth) -> list[dict[str, Any]]:
        return await repo.events(run_id)

    @app.get("/runs/{run_id}/audit-verification")
    async def verify_audit(run_id: str, _role: Auth) -> dict[str, bool]:
        run = await repo.get_run(run_id)
        return {"valid": verify_chain(await repo.events(run_id), run.event_head)}

    @app.get("/runs/{run_id}/report")
    async def report(run_id: str, _role: Auth) -> dict[str, Any]:
        run = await repo.get_run(run_id)
        if run.report is None:
            raise HTTPException(409, "report not ready; run a worker or resolve pending approval")
        return run.report

    @app.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel(run_id: str, actor: Auth) -> dict[str, Any]:
        return await repo.cancel(run_id, actor)

    @app.get("/approvals")
    async def approvals(_role: Auth) -> list[dict[str, Any]]:
        return await repo.approvals()

    @app.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str, value: DecisionInput, actor: Auth) -> dict[str, Any]:
        return await repo.decide(approval_id, actor, "approved", value.reason)

    @app.post("/approvals/{approval_id}/deny")
    async def deny(approval_id: str, value: DecisionInput, actor: Auth) -> dict[str, Any]:
        return await repo.decide(approval_id, actor, "denied", value.reason)

    return app
