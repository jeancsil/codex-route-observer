import asyncio
import gzip
import json
from pathlib import Path

import httpx
import pytest
import zstandard
from fastapi.testclient import TestClient

from codex_route_observer.app import create_app, forward_headers
from codex_route_observer.classifier import choice
from codex_route_observer.cli import codex_command
from codex_route_observer.context import ContextBuilder, decode_payload
from codex_route_observer.settings import Settings, load_policy
from codex_route_observer.store import Store

ROOT = Path(__file__).resolve().parents[1]
MODEL = "gpt-6.1-sol"


def settings(tmp_path, **kwargs):
    return Settings(db_path=tmp_path / "test.sqlite3", policy_path=ROOT / "policy.toml", **kwargs)


def verdict(questions, selected="gpt-6-luna", confidence=0.91):
    answers = {}
    selected_values = {"model": selected, "category": "explanation", "complexity": "routine"}
    for key, question in questions.items():
        options = question["criteria"]
        chosen = selected_values[key]
        answers[key] = {
            "choice": chosen,
            "confidence": confidence,
            "probabilities": {k: 1.0 if k == chosen else 0.0 for k in options},
        }
    return {"model": "jev-test", "answers": answers}


def classify_handler(request):
    body = json.loads(request.content)
    assert MODEL not in body["state"]
    return httpx.Response(200, json=verdict(body["questions"]))


def wait_for(client, request_id, status="complete"):
    async def wait():
        for _ in range(100):
            row = client.app.state.store.get(request_id)
            if row["classification_status"] == status:
                return row
            await asyncio.sleep(0.01)
        raise AssertionError(f"Classification did not reach {status}")

    return client.portal.call(wait)


def test_http_forwards_original_request_and_observes_verdict(tmp_path):
    body = {
        "model": MODEL,
        "input": "Explain this function",
        "stream": True,
        "reasoning": {"effort": "high"},
        "tools": [{"type": "function", "name": "shell"}],
    }
    raw = json.dumps(body, indent=3).encode()
    received = []
    events = [
        {"type": "response.created", "response": {"id": "r1", "model": MODEL}},
        {
            "type": "response.output_item.added",
            "item": {"type": "function_call", "call_id": "call1"},
        },
        {
            "type": "response.completed",
            "response": {
                "id": "r1",
                "model": MODEL,
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 14,
                    "input_tokens_details": {"cached_tokens": 90},
                },
            },
        },
    ]
    sse = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)

    def upstream(request):
        received.append(request)
        return httpx.Response(
            200,
            content=sse,
            headers={"content-type": "text/event-stream", "x-request-id": "upstream-id"},
        )

    app = create_app(
        settings(tmp_path, api_key="test-key"),
        httpx.MockTransport(upstream),
        httpx.MockTransport(classify_handler),
    )
    with TestClient(app) as client:
        response = client.post(
            "/backend-api/codex/responses?test=one",
            content=raw,
            headers={
                "authorization": "Bearer test",
                "chatgpt-account-id": "account",
                "session_id": "session-1",
            },
        )
        assert response.content == sse
        assert response.headers["x-request-id"] == "upstream-id"
        assert received[0].content == raw
        assert str(received[0].url) == "https://chatgpt.com/backend-api/codex/responses?test=one"
        assert received[0].headers["authorization"] == "Bearer test"
        assert received[0].headers["chatgpt-account-id"] == "account"
        rows = client.get("/api/requests").json()["items"]
        row = wait_for(client, rows[0]["id"])
        assert row["requested_model"] == row["reported_model"] == MODEL
        assert row["recommended_model"] == "gpt-6-luna" and row["mismatch"] == 1
        assert row["input_tokens"] == 120 and row["cached_tokens"] == 90
        assert row["delivery_status"] == "completed" and row["reasoning_effort"] == "high"
        assert row["session_id"] == "session-1"
        assert "authorization" not in row
        assert (
            client.get("/api/requests?mismatch=true&category=explanation").json()["summary"][
                "total"
            ]
            == 1
        )
        assert client.get("/api/requests?mismatch=false").json()["summary"]["total"] == 0


def test_classifier_failure_does_not_fail_upstream(tmp_path):
    def upstream(request):
        return httpx.Response(
            429, json={"error": {"type": "rate_limit_exceeded"}}, headers={"retry-after": "2"}
        )

    def broken(request):
        raise httpx.ReadTimeout("fake timeout")

    app = create_app(
        settings(tmp_path, api_key="test"),
        httpx.MockTransport(upstream),
        httpx.MockTransport(broken),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={"model": MODEL, "input": "Explain this"},
            headers={"authorization": "Bearer test"},
        )
        assert response.status_code == 429 and response.headers["retry-after"] == "2"
        assert response.json() == {"error": {"type": "rate_limit_exceeded"}}
        row = client.get("/api/requests").json()["items"][0]
        row = wait_for(client, row["id"], "error")
        assert row["classification_error"] == "ReadTimeout"
        assert row["delivery_status"] == "upstream_error"
        assert row["recommended_model"] is None


def test_slow_classifier_does_not_gate_forwarding(tmp_path):
    async def slow(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json={})

    app = create_app(
        settings(tmp_path, api_key="test"),
        httpx.MockTransport(lambda r: httpx.Response(200, json={"model": MODEL})),
        httpx.MockTransport(slow),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/responses",
            json={"model": MODEL, "input": "Hello"},
            headers={"authorization": "Bearer test"},
        )
        assert response.status_code == 200
        row = client.get("/api/requests").json()["items"][0]
        assert row["classification_status"] == "pending"
        assert row["delivery_status"] == "completed"


def test_disabled_classification_and_model_passthrough(tmp_path):
    paths = []

    def upstream(request):
        paths.append(str(request.url))
        return httpx.Response(200, json={"models": [{"slug": MODEL}]})

    with TestClient(
        create_app(settings(tmp_path, api_key=""), httpx.MockTransport(upstream))
    ) as client:
        assert (
            client.get(
                "/backend-api/codex/models?client_version=0.163.0",
                headers={"authorization": "Bearer test"},
            ).json()["models"][0]["slug"]
            == MODEL
        )
        assert client.get("/api/requests").json()["summary"]["total"] == 0
        client.post(
            "/v1/responses",
            json={"model": MODEL, "input": "Hello"},
            headers={"authorization": "Bearer test"},
        )
        assert client.get("/api/requests").json()["items"][0]["classification_status"] == "disabled"
        assert not client.get("/api/config").json()["classification_enabled"]
        assert paths[0].endswith("models?client_version=0.163.0")
        assert client.get("/backend-api/codex/models").status_code == 401
        assert client.get("/").status_code == 200


def test_terminal_failure_and_incomplete_sse(tmp_path):
    chunks = iter(
        [
            b'data: {"type":"response.failed","response":{"id":"r1"}}\n\n',
            b'data: {"type":"response.created","response":{"id":"r2"}}\n\n',
        ]
    )

    def upstream(request):
        return httpx.Response(
            200, content=next(chunks), headers={"content-type": "text/event-stream"}
        )

    with TestClient(
        create_app(settings(tmp_path, api_key=""), httpx.MockTransport(upstream))
    ) as client:
        for _ in range(2):
            client.post(
                "/v1/responses",
                json={"model": MODEL, "input": "Hello"},
                headers={"authorization": "Bearer test"},
            )
        rows = client.get("/api/requests").json()["items"]
        assert {r["delivery_status"] for r in rows} == {"upstream_error", "incomplete"}


def test_compressed_payload_inspected_but_forwarded_identically(tmp_path):
    original = json.dumps({"model": MODEL, "input": "Explain compression"}).encode()
    raw = zstandard.ZstdCompressor().compress(original)
    received = []

    def upstream(request):
        received.append((request.content, request.headers["content-encoding"]))
        return httpx.Response(200, json={"model": MODEL})

    with TestClient(
        create_app(settings(tmp_path, api_key=""), httpx.MockTransport(upstream))
    ) as client:
        client.post(
            "/v1/responses",
            content=raw,
            headers={"authorization": "Bearer test", "content-encoding": "zstd"},
        )
        assert received == [(raw, "zstd")]
        assert (
            client.get("/api/requests").json()["items"][0]["prompt_preview"]
            == "Explain compression"
        )
    assert decode_payload(gzip.compress(original), "gzip")["model"] == MODEL
    assert decode_payload(b"broken", "gzip") is None


def test_context_inheritance_redaction_and_bounds():
    builder = ContextBuilder()
    first = builder.build({"input": "Fix the parser. sk-abcdefghijklmnopqrstuvwxyz123456"}, "s1")
    assert "sk-" not in first["user_request"]
    continuation = builder.build(
        {
            "input": [{"type": "function_call_output", "output": "SyntaxError"}],
            "previous_response_id": "r1",
        },
        "s1",
    )
    assert (
        continuation["user_request_inherited"] and "Fix the parser" in continuation["user_request"]
    )
    unknown = builder.build({"input": []}, "other-session")
    assert unknown["context_missing"]
    long = builder.build({"input": [{"role": "user", "content": "z" * 100_000}] * 20}, "long")
    assert long["context_truncated"] and len(json.dumps(long)) <= 24_000


def test_store_filters_are_parameterized_and_restart_marks_pending(tmp_path):
    path = tmp_path / "test.sqlite3"
    store = Store(path)
    first = store.insert(
        session_id="a",
        transport="http",
        endpoint="/v1/responses",
        prompt_preview="100% done",
        requested_model=MODEL,
    )
    store.insert(
        session_id="b",
        transport="websocket",
        endpoint="/responses",
        prompt_preview="1000 done",
        requested_model="gpt-6-astra",
    )
    store.update(
        first,
        classification_status="complete",
        confidence=0.9,
        category="writing",
        mismatch=1,
        recommended_model="gpt-6-luna",
        delivery_status="completed",
    )
    assert store.query({"q": "%"})["summary"]["total"] == 1
    assert store.query({"requested_model": "' OR 1=1 --"})["summary"]["total"] == 0
    assert store.query({"min_confidence": 0.95})["summary"]["total"] == 0
    assert store.query({"session_id": "a", "mismatch": True})["summary"]["mismatches"] == 1
    assert len(store.query({}, limit=1)["items"]) == 1
    store.close()
    store = Store(path)
    assert store.query({"classification_status": "interrupted"})["summary"]["total"] == 1
    store.close()


@pytest.mark.parametrize(
    "answer",
    [
        {"choice": "unknown", "confidence": 0.9, "probabilities": {"a": 1.0, "b": 0.0}},
        {"choice": "a", "confidence": float("nan"), "probabilities": {"a": 1.0, "b": 0.0}},
        {"choice": "a", "confidence": 0.9, "probabilities": {"a": 0.1, "b": 0.1}},
    ],
)
def test_invalid_verdict_rejected(answer):
    with pytest.raises(ValueError):
        choice(answer, {"a": "first", "b": "second"})


def test_launcher_only_overrides_provider_and_preserves_user_args():
    command = codex_command(["exec", "--model", MODEL, "hello"], 9393, "chatgpt")
    assert command[-4:] == ["exec", "--model", MODEL, "hello"]
    assert any("requires_openai_auth=true" in x for x in command)
    assert any("supports_websockets=true" in x for x in command)
    assert any('base_url="http://127.0.0.1:9393/backend-api/codex"' in x for x in command)
    assert not any(x.startswith("model=") for x in command)
    assert any('env_key="OPENAI_API_KEY"' in x for x in codex_command([], 9393, "api"))


def test_hop_headers_and_policy():
    headers = dict(
        forward_headers(
            httpx.Headers(
                {
                    "authorization": "Bearer safe",
                    "connection": "x-hop",
                    "x-hop": "drop",
                    "host": "localhost",
                    "chatgpt-account-id": "keep",
                }
            )
        )
    )
    assert headers == {"authorization": "Bearer safe", "chatgpt-account-id": "keep"}
    policy = load_policy(ROOT / "policy.toml")
    assert len(policy["models"]) == 3


def test_websocket_frames_continuation_and_metadata(tmp_path, monkeypatch):
    import codex_route_observer.app as module

    sent = []
    connections = []

    class FakeUpstream:
        subprotocol = None

        async def __aenter__(self):
            self.queue = asyncio.Queue()
            return self

        async def __aexit__(self, *args):
            pass

        async def send(self, raw):
            sent.append(raw)
            payload = json.loads(raw)
            if payload.get("type") != "response.create":
                return
            response = {"id": f"r{len(sent)}", "model": MODEL}
            await self.queue.put(json.dumps({"type": "response.created", "response": response}))
            await self.queue.put(
                json.dumps(
                    {
                        "type": "response.completed",
                        "response": {
                            **response,
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                        },
                    }
                )
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.queue.get()

    def fake_connect(url, **kwargs):
        connections.append((url, kwargs))
        return FakeUpstream()

    monkeypatch.setattr(module, "connect", fake_connect)
    app = create_app(
        settings(tmp_path, api_key="test"),
        classifier_transport=httpx.MockTransport(classify_handler),
    )
    with TestClient(app) as client:
        with client.websocket_connect(
            "/backend-api/codex/responses",
            headers={"authorization": "Bearer test", "session_id": "ws-session"},
        ) as socket:
            first = json.dumps(
                {"type": "response.create", "model": MODEL, "input": "Explain the parser"}, indent=2
            )
            socket.send_text(first)
            assert socket.receive_json()["type"] == "response.created"
            assert socket.receive_json()["type"] == "response.completed"
            followup = json.dumps(
                {
                    "type": "response.create",
                    "model": MODEL,
                    "previous_response_id": "r1",
                    "input": [
                        {"type": "function_call_output", "call_id": "call-1", "output": "ok"}
                    ],
                }
            )
            socket.send_text(followup)
            assert socket.receive_json()["response"]["id"] == "r2"
            assert socket.receive_json()["type"] == "response.completed"
        assert sent == [first, followup]
        assert connections[0][0] == "wss://chatgpt.com/backend-api/codex/responses"
        assert dict(connections[0][1]["additional_headers"])["authorization"] == "Bearer test"
        rows = client.get("/api/requests").json()["items"]
        assert len(rows) == 2
        for row in rows:
            row = wait_for(client, row["id"])
            assert row["delivery_status"] == "completed"
            assert row["reported_model"] == MODEL and row["output_tokens"] == 5
            assert row["session_id"] == "ws-session"
            assert row["prompt_preview"] == "Explain the parser"


def test_turn_metadata_provides_stable_session_and_date_filters_normalize(tmp_path):
    app = create_app(
        settings(tmp_path, api_key=""),
        httpx.MockTransport(lambda r: httpx.Response(200, json={"model": MODEL})),
    )
    with TestClient(app) as client:
        client.post(
            "/v1/responses",
            json={"model": MODEL, "input": "Explain this"},
            headers={
                "authorization": "Bearer test",
                "x-codex-turn-metadata": json.dumps({"thread_id": "stable-thread"}),
            },
        )
        row = client.get("/api/requests").json()["items"][0]
        assert row["session_id"] == "stable-thread"
        app.state.store.update(row["id"], created_at="2026-10-18T10:00:00.000+00:00")
        params = {"after": "2026-10-18T12:00:00+02:00", "before": "2026-10-18T10:00:00Z"}
        assert client.get("/api/requests", params=params).json()["summary"]["total"] == 1
        assert client.get("/api/requests?after=not-a-date").status_code == 422
