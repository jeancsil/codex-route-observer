import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_observer import MODEL, settings, verdict, wait_for

from codex_route_observer.app import create_app
from codex_route_observer.jev_usage import PRICING, usage_values, usd
from codex_route_observer.store import Store


def observe(client):
    response = client.post(
        "/v1/responses",
        json={"model": MODEL, "input": "Explain this function"},
        headers={"authorization": "Bearer test", "session_id": "session-test"},
    )
    assert response.status_code == 200
    return client.get("/api/requests").json()["items"][0]["id"]


def test_real_usage_price_response_and_observability_make_no_extra_calls(tmp_path):
    calls = []
    raw_response = {}

    def fake_jev(request):
        calls.append(request)
        raw_response.update(verdict(json.loads(request.content)["questions"]))
        raw_response.update(model="jev-1.13.0", usage={"input_tokens": 1234, "output_tokens": 56})
        return httpx.Response(200, json=raw_response, headers={"x-request-id": "provider-123"})

    app = create_app(
        settings(tmp_path, api_key="secret-test"),
        httpx.MockTransport(lambda r: httpx.Response(200, json={"model": MODEL})),
        httpx.MockTransport(fake_jev),
    )
    with TestClient(app) as client:
        request_id = observe(client)
        wait_for(client, request_id)
        data = client.get("/api/jev-calls").json()
        assert data["summary"]["total"] == data["summary"]["priced"] == 1
        assert data["summary"]["cost_usd"] == "0.000051828"
        row = data["items"][0]
        assert row["question_count"] == 3 and row["input_tokens"] == 1234
        assert row["output_tokens"] == 56 and row["duration_ms"] >= 0 and row["queue_ms"] >= 0
        assert row["request_bytes"] == len(calls[0].content)
        assert row["response_bytes"] > 0
        assert row["codex_model"] == MODEL and row["session_id"] == "session-test"
        assert row["pricing_source"] == PRICING["source"]
        assert "response_text" not in row and "usage_json" not in row
        detail = client.get("/api/jev-calls/" + row["id"]).json()
        assert json.loads(detail["response_text"]) == raw_response
        assert detail["provider_request_id"] == "provider-123"
        assert detail["cost_usd"] == "0.000051828"
        assert client.get("/api/jev-calls?status=error").json()["summary"]["total"] == 0
        assert (
            client.get("/api/jev-calls?after=2099-01-01T00:00:00Z").json()["summary"]["total"] == 0
        )
        client.get("/api/config")
        client.get("/api/jev-calls")
        assert len(calls) == 1, "Inspecting telemetry must never invoke Jev"
        assert client.get("/api/jev-calls/nonexistent").status_code == 404
        assert client.get("/api/jev-calls?status=invalid").status_code == 422


@pytest.mark.parametrize("failure", ["timeout", "rate_limit", "malformed_verdict"])
def test_failed_calls_are_recorded_and_billable_usage_survives_validation_failure(
    tmp_path, failure
):
    def fake_jev(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("do not persist this raw message")
        if failure == "rate_limit":
            return httpx.Response(429, json={"error": "rate limit"})
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "usage": {"input_tokens": 1000, "output_tokens": 0},
                "answers": {},
            },
        )

    app = create_app(
        settings(tmp_path, api_key="test"),
        httpx.MockTransport(lambda r: httpx.Response(200, json={"model": MODEL})),
        httpx.MockTransport(fake_jev),
    )
    with TestClient(app) as client:
        wait_for(client, observe(client), "error")
        data = client.get("/api/jev-calls").json()
        assert data["summary"]["total"] == data["summary"]["errors"] == 1
        row = data["items"][0]
        assert row["status"] == "error" and row["duration_ms"] >= 0
        if failure == "malformed_verdict":
            assert row["http_status"] == 200
            assert row["cost_usd"] == "0.000042000"
        else:
            assert row["cost_usd"] is None and data["summary"]["cost_usd"] is None
            assert data["summary"]["unpriced"] == 1
        if failure == "timeout":
            assert row["response_bytes"] is None
            assert row["error_type"] == "ReadTimeout"


def test_unknown_model_gateway_and_bad_usage_are_never_priced_as_zero():
    base = {"model": "jev-1.13.0", "usage": {"input_tokens": 1, "output_tokens": 5000}}
    assert usd(usage_values(base, "https://api.typesafe.ai")["cost_nusd"]) == "0.000000042"
    for data, endpoint in [
        ({**base, "model": "jev-future"}, "https://api.typesafe.ai"),
        (base, "https://gateway.example.com"),
        ({**base, "usage": {"input_tokens": True}}, "https://api.typesafe.ai"),
        ({**base, "usage": {"input_tokens": -1}}, "https://api.typesafe.ai"),
        ({**base, "usage": {}}, "https://api.typesafe.ai"),
    ]:
        assert usage_values(data, endpoint)["cost_nusd"] is None
    assert (
        usage_values({**base, "model": "jev-1.14.0"}, "https://api.typesafe.ai")["cost_nusd"] == 42
    )


def test_precise_sums_partial_coverage_percentile_and_restart(tmp_path):
    path = tmp_path / "accounting.sqlite3"
    store = Store(path)
    request_id = store.insert(
        session_id="test", transport="http", endpoint="/responses", classification_status="complete"
    )
    ids = []
    for duration in range(1, 21):
        ids.append(
            store.insert_jev(
                request_id=request_id,
                requested_model="jev-latest",
                status="success",
                duration_ms=duration,
                cost_nusd=42,
                input_tokens=1,
            )
        )
    store.insert_jev(
        request_id=request_id, requested_model="jev-latest", status="error", duration_ms=100
    )
    pending = store.insert_jev(request_id=request_id, requested_model="jev-latest")
    summary = store.query_jev()["summary"]
    assert summary["cost_usd"] == "0.000000840"
    assert summary["priced"] == 20 and summary["unpriced"] == 2
    assert summary["p95_duration_ms"] == 20
    assert summary["legacy_classifications"] == 0
    assert len(store.query_jev(limit=2, offset=20)["items"]) == 2
    store.close()
    store = Store(path)
    assert store.get_jev(pending)["status"] == "interrupted"
    assert store.get_jev(ids[0])["cost_usd"] == "0.000000042"
    store.close()


def test_additive_migration_preserves_old_classifications_without_inventing_costs(tmp_path):
    path = tmp_path / "old.sqlite3"
    store = Store(path)
    request_id = store.insert(
        session_id="historical",
        transport="http",
        endpoint="/responses",
        classification_status="complete",
        classifier_model="jev-1.13.0",
    )
    store.conn.execute("DROP TABLE jev_calls")
    store.conn.commit()
    store.close()
    store = Store(path)
    assert store.get(request_id)["session_id"] == "historical"
    summary = store.query_jev()["summary"]
    assert summary["total"] == 0 and summary["cost_usd"] is None
    assert summary["legacy_classifications"] == 1
    store.close()


def test_responses_are_redacted_bounded_and_not_synthesized_for_disabled_calls(tmp_path):
    def fake_jev(request):
        return httpx.Response(400, text="sk-abcdefghijklmnopqrstuvwxyz123456 " + "x" * 70000)

    app = create_app(
        settings(tmp_path, api_key="test"),
        httpx.MockTransport(lambda r: httpx.Response(200, json={"model": MODEL})),
        httpx.MockTransport(fake_jev),
    )
    with TestClient(app) as client:
        wait_for(client, observe(client), "error")
        row = client.get("/api/jev-calls").json()["items"][0]
        detail = client.get("/api/jev-calls/" + row["id"]).json()
        assert detail["response_truncated"] == 1
        assert "sk-" not in detail["response_text"]
        assert detail["response_bytes"] > len(detail["response_text"])
    app = create_app(
        settings(tmp_path, api_key=""),
        httpx.MockTransport(lambda r: httpx.Response(200, json={"model": MODEL})),
    )
    with TestClient(app) as client:
        wait_for(client, observe(client), "disabled")
        assert client.get("/api/jev-calls").json()["summary"]["total"] == 1
