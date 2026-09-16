import json
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from aegisops.api.app import create_app
from aegisops.config import Settings
from aegisops.domain import HypothesisOutput, IncidentInput, Plan, RemoteFailure
from aegisops.llm.providers import FakeDeterministicProvider, OpenAICompatibleProvider
from aegisops.runtime.worker import Worker
from aegisops.tools.sources import bounded_json


async def test_http_create_investigate_report(repo):
    app = create_app(repo.cfg, repo.db)
    headers = {"Authorization": "Bearer " + repo.cfg.viewer_token.get_secret_value()}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/ready")).status_code == 200
        assert (await client.post("/incidents", json={"title": "test"})).status_code == 401
        incident = (await client.post("/incidents", json={"title": "test"}, headers=headers)).json()
        response = await client.post(f"/incidents/{incident['id']}/investigate", headers=headers)
        assert response.status_code == 202
        run = response.json()
        assert (await client.get(f"/runs/{run['id']}/report", headers=headers)).status_code == 409
        await Worker(repo, repo.cfg).until_done(run["id"])
        report = await client.get(f"/runs/{run['id']}/report", headers=headers)
        assert report.status_code == 200 and report.json()["evidence"]
        assert (await client.get(f"/runs/{run['id']}/audit-verification", headers=headers)).json()[
            "valid"
        ]
        metrics = (await client.get("/metrics")).text
        assert "aegis_tool_calls_total" in metrics
        assert "aegis_runs_total 1" in metrics
        assert "# TYPE aegis_runs gauge" in metrics
        assert (
            await client.post(
                "/incidents", json={"title": "test", "scenario": "../x"}, headers=headers
            )
        ).status_code == 422


async def test_openai_compatible_contract():
    fake = FakeDeterministicProvider()
    expected, _ = await fake.generate("plan", {"incident": {"service": "order-service"}}, Plan)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["response_format"]["type"] == "json_object"
        assert "UNTRUSTED_DATA" in body["messages"][1]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": expected.model_dump_json()}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 100},
            },
        )

    cfg = Settings(_env_file=None, provider="openai", llm_api_key="test-key")
    actual, usage = await OpenAICompatibleProvider(cfg, httpx.MockTransport(handler)).generate(
        "plan", {}, Plan
    )
    assert actual == expected and usage["prompt_tokens"] == 80


@pytest.mark.parametrize(
    "status,body,category",
    [
        (429, b"{}", "http_429"),
        (503, b"{}", "http_5xx"),
        (200, b"{bad", "malformed_json"),
        (302, b"{}", "http_rejected"),
        (200, b"x" * 3000, "response_too_large"),
    ],
)
async def test_http_failures_and_body_limit(status, body, category):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, content=body))
    ) as client:
        with pytest.raises(RemoteFailure) as error:
            await bounded_json(client, "GET", "http://allowed.example/", 2048)
        assert error.value.category == category


class InvalidCitationProvider(FakeDeterministicProvider):
    async def generate(self, task: str, data: dict[str, Any], schema: type[BaseModel]):
        result, usage = await super().generate(task, data, schema)
        if isinstance(result, HypothesisOutput):
            result.top_root_cause.cause = "downstream_timeout"
            result.top_root_cause.evidence_ids = ["fabricated-reference"]
        return result, usage


async def test_self_correction_exhaustion_abstains(repo):
    incident = await repo.create_incident(IncidentInput(title="test"))
    run = await repo.start(incident.id)
    result = await Worker(repo, repo.cfg, provider=InvalidCitationProvider()).until_done(run.id)
    assert result["state"] == "COMPLETED"
    assert result["context"]["corrections"] == 1
    assert result["report"]["top_root_cause"]["cause"] == "insufficient_evidence"
