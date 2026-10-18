import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from .classifier import Classifier
from .context import ContextBuilder, decode_payload
from .jev_usage import PRICING
from .settings import Settings, load_policy
from .store import Store

log = logging.getLogger("codex_route_observer")
STATIC = Path(__file__).parent / "static"
HOP_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


def forward_headers(headers, websocket=False):
    excluded = HOP_HEADERS | {x.strip().lower() for x in headers.get("connection", "").split(",")}
    if websocket:
        excluded |= {
            "sec-websocket-key",
            "sec-websocket-version",
            "sec-websocket-extensions",
            "sec-websocket-protocol",
            "accept-encoding",
        }
    return [(k, v) for k, v in headers.items() if k.lower() not in excluded]


def upstream_url(settings, mode, path, query="", websocket=False):
    if path.startswith("/") or any(p in {"..", "."} for p in path.split("/")):
        raise ValueError("Invalid upstream path")
    base = settings.chatgpt_upstream if mode == "chatgpt" else settings.api_upstream
    url = base.rstrip("/") + "/" + path
    if query:
        url += "?" + query
    if websocket:
        url = url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return url


class Observation:
    def __init__(self, store, classifier, contexts):
        self.store, self.classifier, self.contexts = store, classifier, contexts

    def record(self, payload, headers, transport, endpoint, size, fallback_session=None):
        try:
            return self._record(payload, headers, transport, endpoint, size, fallback_session)
        except Exception as exc:
            log.error("Observation unavailable: %s", type(exc).__name__)
            return None

    def _record(self, payload, headers, transport, endpoint, size, fallback_session=None):
        if not payload:
            return None
        try:
            metadata = json.loads(headers.get("x-codex-turn-metadata", "{}"))
            if not isinstance(metadata, dict):
                metadata = {}
        except ValueError:
            metadata = {}
        session = (
            headers.get("session_id")
            or headers.get("x-codex-session-id")
            or headers.get("conversation_id")
            or headers.get("x-session-id")
            or metadata.get("thread_id")
            or metadata.get("session_id")
            or fallback_session
            or "unattributed-" + uuid4().hex[:12]
        )
        state = self.contexts.build(payload, session)
        reasoning = payload.get("reasoning")
        try:
            request_id = self.store.insert(
                session_id=session,
                transport=transport,
                endpoint=endpoint,
                requested_model=payload.get("model"),
                reasoning_effort=reasoning.get("effort") if isinstance(reasoning, dict) else None,
                prompt_preview=state["user_request"][-1200:],
                request_bytes=size,
                context_truncated=int(state["context_truncated"]),
                context_missing=int(state["context_missing"]),
                policy_version=self.classifier.policy["version"],
            )
            self.classifier.submit(request_id, state)
            return request_id
        except Exception as exc:
            log.error("Observation unavailable: %s", type(exc).__name__)
            return None

    def update(self, request_id, **values):
        if request_id:
            try:
                self.store.update(request_id, **values)
            except Exception as exc:
                log.error("Observation update unavailable: %s", type(exc).__name__)


class Events:
    """Read metadata alongside a stream; never rewrite upstream events."""

    def __init__(self, observation, request_id):
        self.observation, self.request_id = observation, request_id
        self.buffer = b""
        self.start = time.perf_counter()
        self.first = True
        self.terminal = False

    def elapsed(self):
        return round((time.perf_counter() - self.start) * 1000, 1)

    def feed(self, chunk):
        if self.first:
            self.observation.update(self.request_id, first_byte_ms=self.elapsed())
            self.first = False
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if line.startswith(b"data:"):
                try:
                    self.event(json.loads(line[5:].strip()))
                except (ValueError, TypeError):
                    pass
        # Metadata observation must not retain arbitrarily large output lines.
        if len(self.buffer) > 1_048_576:
            self.buffer = b""

    def event(self, event):
        if not isinstance(event, dict):
            return
        response = event.get("response", event)
        if not isinstance(response, dict):
            return
        values = {}
        if isinstance(response.get("model"), str):
            values["reported_model"] = response["model"]
        usage = response.get("usage")
        if isinstance(usage, dict):
            values.update(
                input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens")
            )
            details = usage.get("input_tokens_details") or {}
            values["cached_tokens"] = (
                details.get("cached_tokens") if isinstance(details, dict) else None
            )
        kind = event.get("type", "")
        if kind in {"response.completed", "response.failed", "response.incomplete", "error"}:
            self.terminal = True
            values["delivery_status"] = (
                "completed" if kind == "response.completed" else "upstream_error"
            )
            values["latency_ms"] = self.elapsed()
            if kind != "response.completed":
                values["upstream_error"] = kind
        self.observation.update(self.request_id, **values)


def create_app(settings=None, upstream_transport=None, classifier_transport=None):
    settings = settings or Settings()
    policy = load_policy(settings.policy_path)

    @asynccontextmanager
    async def lifespan(app):
        store = Store(settings.db_path)
        classifier = Classifier(settings, policy, store, classifier_transport)
        classifier.start()
        app.state.store = store
        app.state.classifier = classifier
        app.state.observation = Observation(store, classifier, ContextBuilder())
        app.state.http = httpx.AsyncClient(
            timeout=httpx.Timeout(30, read=300),
            transport=upstream_transport,
            follow_redirects=False,
        )
        try:
            yield
        finally:
            await app.state.http.aclose()
            await classifier.stop()
            store.close()

    app = FastAPI(title="Codex Route Observer", lifespan=lifespan)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    async def dashboard():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health():
        return {
            "ok": True,
            "service": "codex-route-observer",
            "observation_only": True,
            "classification_enabled": bool(settings.api_key),
        }

    @app.get("/api/config")
    async def config():
        return {
            "policy": policy,
            "classification_enabled": bool(settings.api_key),
            "classifier_model": settings.typesafe_model,
            "queue_depth": app.state.classifier.queue.qsize(),
            "mode": "observe",
            "jev_pricing": PRICING,
            "upstreams": {"chatgpt": settings.chatgpt_upstream, "api": settings.api_upstream},
        }

    @app.get("/api/facets")
    async def facets():
        return app.state.store.facets()

    @app.get("/api/jev-calls")
    async def jev_calls(
        status: Literal["success", "error", "pending", "interrupted"] | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        limit: int = Query(20, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        def normalized(value):
            return (
                value.replace(tzinfo=value.tzinfo or UTC)
                .astimezone(UTC)
                .isoformat(timespec="milliseconds")
                if value
                else None
            )

        return app.state.store.query_jev(
            status, normalized(after), normalized(before), limit, offset
        )

    @app.get("/api/jev-calls/{call_id}")
    async def jev_call_detail(call_id: str):
        row = app.state.store.get_jev(call_id)
        if not row:
            raise HTTPException(404, "Jev call not found")
        return row

    @app.get("/api/requests")
    async def requests(
        requested_model: str | None = None,
        recommended_model: str | None = None,
        category: str | None = None,
        classification_status: str | None = None,
        session_id: str | None = None,
        transport: str | None = None,
        delivery_status: str | None = None,
        mismatch: bool | None = None,
        q: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        min_confidence: float | None = Query(None, ge=0, le=1),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        filters = {
            "requested_model": requested_model,
            "recommended_model": recommended_model,
            "category": category,
            "classification_status": classification_status,
            "session_id": session_id,
            "transport": transport,
            "delivery_status": delivery_status,
            "mismatch": mismatch,
            "q": q,
            "after": after.replace(tzinfo=after.tzinfo or UTC)
            .astimezone(UTC)
            .isoformat(timespec="milliseconds")
            if after
            else None,
            "before": before.replace(tzinfo=before.tzinfo or UTC)
            .astimezone(UTC)
            .isoformat(timespec="milliseconds")
            if before
            else None,
            "min_confidence": min_confidence,
        }
        return app.state.store.query(filters, limit, offset)

    @app.get("/api/requests/{request_id}")
    async def request_detail(request_id: str):
        row = app.state.store.get(request_id)
        if not row:
            raise HTTPException(404, "Request not found")
        return row

    async def proxy_http(request: Request, path: str, mode: str):
        try:
            url = upstream_url(settings, mode, path, request.url.query)
        except ValueError:
            raise HTTPException(400, "Invalid path") from None
        # No auth.json access: credentials arrive from Codex and are only relayed.
        if not request.headers.get("authorization"):
            return JSONResponse(
                {"error": "Codex authorization header is required"}, status_code=401
            )
        raw = await request.body()
        obs = app.state.observation
        request_id = None
        if request.method == "POST" and path.startswith("responses"):
            payload = decode_payload(raw, request.headers.get("content-encoding", ""))
            request_id = obs.record(payload, request.headers, "http", request.url.path, len(raw))
        events = Events(obs, request_id)
        upstream_request = app.state.http.build_request(
            request.method,
            url,
            headers=forward_headers(request.headers),
            content=raw,
        )
        try:
            response = await app.state.http.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            obs.update(
                request_id,
                delivery_status="upstream_error",
                upstream_error=type(exc).__name__,
                status_code=502,
                latency_ms=events.elapsed(),
            )
            return JSONResponse(
                {"error": "Upstream connection failed", "type": type(exc).__name__}, status_code=502
            )
        obs.update(
            request_id,
            status_code=response.status_code,
            delivery_status="upstream_error" if response.is_error else "streaming",
        )
        is_sse = "text/event-stream" in response.headers.get("content-type", "")

        async def stream():
            json_body = bytearray()
            exhausted = False
            try:
                async for chunk in response.aiter_bytes():
                    if is_sse:
                        events.feed(chunk)
                    elif len(json_body) < 1_048_576:
                        json_body.extend(chunk[: 1_048_576 - len(json_body)])
                    yield chunk
                exhausted = True
                if not is_sse and json_body:
                    try:
                        events.event(json.loads(json_body))
                    except (ValueError, TypeError):
                        pass
                if not events.terminal:
                    if response.is_error:
                        status = "upstream_error"
                    elif is_sse:
                        status = "incomplete"
                    else:
                        status = "completed"
                    obs.update(request_id, delivery_status=status, latency_ms=events.elapsed())
            finally:
                if not exhausted and not events.terminal:
                    obs.update(
                        request_id, delivery_status="disconnected", latency_ms=events.elapsed()
                    )
                await response.aclose()

        # httpx decodes the response body incrementally; drop its old encoding/length.
        headers = {
            k: v
            for k, v in response.headers.items()
            if k.lower() not in HOP_HEADERS | {"content-encoding"}
        }
        return StreamingResponse(stream(), status_code=response.status_code, headers=headers)

    @app.api_route("/backend-api/codex/{path:path}", methods=["GET", "POST", "DELETE"])
    async def chatgpt_http(request: Request, path: str):
        return await proxy_http(request, path, "chatgpt")

    @app.api_route("/v1/{path:path}", methods=["GET", "POST", "DELETE"])
    async def api_http(request: Request, path: str):
        return await proxy_http(request, path, "api")

    async def proxy_ws(socket: WebSocket, path: str, mode: str):
        if not socket.headers.get("authorization"):
            await socket.close(code=1008)
            return
        origin = socket.headers.get("origin")
        if origin and origin not in {
            f"http://{socket.headers['host']}",
            f"https://{socket.headers['host']}",
        }:
            await socket.close(code=1008)
            return
        try:
            url = upstream_url(settings, mode, path, socket.url.query, websocket=True)
        except ValueError:
            await socket.close(code=1008)
            return
        obs = app.state.observation
        pending, active, all_events = deque(), {}, []
        connection_session = "ws-" + uuid4().hex[:12]
        last_model = None
        try:
            async with connect(
                url,
                additional_headers=forward_headers(socket.headers, websocket=True),
                subprotocols=socket.scope.get("subprotocols") or None,
                user_agent_header=None,
                max_size=None,
                compression=None,
                open_timeout=30,
                close_timeout=5,
            ) as upstream:
                await socket.accept(subprotocol=upstream.subprotocol)

                async def to_upstream():
                    nonlocal last_model
                    while True:
                        message = await socket.receive()
                        if message["type"] == "websocket.disconnect":
                            return
                        raw = message.get("text")
                        binary = message.get("bytes")
                        payload = decode_payload(raw.encode() if raw is not None else binary or b"")
                        if payload and payload.get("type") == "response.create":
                            last_model = payload.get("model", last_model)
                            observed_payload = {**payload, "model": last_model}
                            request_id = obs.record(
                                observed_payload,
                                socket.headers,
                                "websocket",
                                socket.url.path,
                                len(raw.encode() if raw is not None else binary or b""),
                                connection_session,
                            )
                            event = Events(obs, request_id)
                            pending.append(event)
                            all_events.append(event)
                            obs.update(request_id, delivery_status="streaming", status_code=101)
                        await upstream.send(raw if raw is not None else binary)

                async def to_client():
                    async for message in upstream:
                        payload = decode_payload(
                            message.encode() if isinstance(message, str) else message
                        )
                        if payload:
                            response = payload.get("response") or {}
                            response_id = response.get("id") if isinstance(response, dict) else None
                            kind = payload.get("type")
                            if kind == "response.created" and pending and response_id:
                                active[response_id] = pending.popleft()
                            event = active.get(response_id)
                            if not event and kind == "error":
                                event = next(iter(active.values()), None)
                                if not event and pending:
                                    event = pending.popleft()
                            if event:
                                if event.first:
                                    obs.update(event.request_id, first_byte_ms=event.elapsed())
                                    event.first = False
                                event.event(payload)
                                if event.terminal:
                                    active.pop(response_id, None)
                        if isinstance(message, str):
                            await socket.send_text(message)
                        else:
                            await socket.send_bytes(message)

                tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (WebSocketDisconnect, ConnectionClosed):
            pass
        except Exception as exc:
            log.warning("WebSocket relay ended: %s", type(exc).__name__)
            for event in all_events:
                if not event.terminal:
                    obs.update(event.request_id, upstream_error=type(exc).__name__)
        finally:
            for event in all_events:
                if not event.terminal:
                    obs.update(
                        event.request_id, delivery_status="disconnected", latency_ms=event.elapsed()
                    )
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await socket.close()

    @app.websocket("/backend-api/codex/{path:path}")
    async def chatgpt_ws(socket: WebSocket, path: str):
        await proxy_ws(socket, path, "chatgpt")

    @app.websocket("/v1/{path:path}")
    async def api_ws(socket: WebSocket, path: str):
        await proxy_ws(socket, path, "api")

    return app
