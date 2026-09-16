import httpx
import pytest

from aegisops.domain import Query
from aegisops.tools.sources import LiveDataSource


async def test_live_adapters_parse_service_apis(repo, monkeypatch):
    original = httpx.AsyncClient
    seen = []

    def route(request):
        seen.append(str(request.url))
        path = request.url.path
        if path == "/api/v1/query":
            return httpx.Response(
                200, json={"status": "success", "data": {"result": [{"value": [1, "0.5"]}]}}
            )
        if path == "/loki/api/v1/query_range":
            return httpx.Response(
                200, json={"data": {"result": [{"values": [["1", "service failed"]]}]}}
            )
        if path == "/api/search":
            return httpx.Response(200, json={"traces": [{"traceID": "a" * 32}]})
        if path.startswith("/api/traces/"):
            return httpx.Response(
                200,
                json={
                    "batches": [
                        {
                            "resource": {
                                "attributes": [
                                    {
                                        "key": "service.name",
                                        "value": {"stringValue": "order-service"},
                                    }
                                ]
                            },
                            "scopeSpans": [
                                {
                                    "spans": [
                                        {
                                            "name": "call",
                                            "startTimeUnixNano": "0",
                                            "endTimeUnixNano": "900000000",
                                            "status": {"code": 2},
                                            "attributes": [
                                                {
                                                    "key": "peer.service",
                                                    "value": {"stringValue": "inventory-service"},
                                                }
                                            ],
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                },
            )
        if path == "/metadata":
            return httpx.Response(
                200, json={"service": "order-service", "version": "v1", "revision": 0}
            )
        raise AssertionError(path)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(route), **kwargs),
    )
    source = LiveDataSource(repo.cfg)
    query = Query(service="order-service")
    for tool in (
        "query_metrics",
        "query_logs",
        "query_traces",
        "get_deploy_metadata",
        "service_topology",
    ):
        output = await source.read(tool, query, 0)
        assert output.records
    assert any(
        "inventory-service" in str(r) for r in (await source.read("query_traces", query, 0)).records
    )
    assert len(seen) <= 12


async def test_demo_control_requires_token_and_changes_real_response(repo, monkeypatch):
    from aegisops.demo_service import create_demo_app

    cfg = repo.cfg.model_copy(update={"demo_only": True, "demo_token": repo.cfg.operator_token})
    app = create_demo_app(cfg, "inventory-service")
    original = httpx.AsyncClient
    # Stub only the Loki export. The /work handler reads the actual SQL database.
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda _: httpx.Response(204)), **kwargs
        ),
    )
    async with original(transport=httpx.ASGITransport(app=app), base_url="http://demo") as client:
        assert (await client.post("/demo/fault", json={"fault": "error"})).status_code == 403
        response = await client.post(
            "/demo/fault",
            json={"fault": "error"},
            headers={"Authorization": "Bearer " + cfg.demo_token.get_secret_value()},
        )
        assert response.status_code == 200
        assert (await client.get("/work")).status_code == 500
        await client.post(
            "/demo/fault",
            json={"fault": "none"},
            headers={"Authorization": "Bearer " + cfg.demo_token.get_secret_value()},
        )
        assert (await client.get("/work")).status_code == 200


def test_demo_service_refuses_default_exposure(repo):
    from aegisops.demo_service import create_demo_app

    with pytest.raises(RuntimeError):
        create_demo_app(repo.cfg, "inventory-service")
