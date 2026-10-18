"""Passive accounting for existing Jev calls; never makes a network request."""

import json
import logging
from decimal import Decimal
from urllib.parse import urlsplit

from .context import redact

log = logging.getLogger("codex_route_observer")
PRICING = {
    "source": "https://docs.typesafe.ai/models",
    "verified_at": "2026-10-18",
    "currency": "USD",
    "basis": "Published list price × API-reported input tokens; excludes credits, discounts, and tax.",
    "input_usd_per_million": "0.042",
    "output_usd_per_million": "0",
    "models": ["jev-1.13.0", "jev-1.14.0"],
}
RESPONSE_LIMIT = 65_536


def usd(nanodollars: int | None) -> str | None:
    return (
        format(Decimal(nanodollars) / Decimal(1_000_000_000), ".9f")
        if nanodollars is not None
        else None
    )


def token_count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def usage_values(data: dict, endpoint: str) -> dict:
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    input_tokens = token_count(usage.get("input_tokens"))
    output_tokens = token_count(usage.get("output_tokens"))
    model = data.get("model") if isinstance(data.get("model"), str) else None
    direct = (
        urlsplit(endpoint).scheme == "https"
        and urlsplit(endpoint).netloc == "api.typesafe.ai"
        and urlsplit(endpoint).path in {"", "/"}
    )
    known = direct and model in PRICING["models"] and input_tokens is not None
    # $0.042 / 1,000,000 tokens = exactly 42 nanodollars per input token.
    # Integer accounting avoids floating-point accumulation and premature rounding.
    return {
        "reported_model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_nusd": input_tokens * 42 if known else None,
        "input_rate_nusd": 42 if known else None,
        "output_rate_nusd": 0 if known else None,
        "pricing_source": PRICING["source"] if known else None,
        "pricing_verified_at": PRICING["verified_at"] if known else None,
    }


class JevMeter:
    def __init__(self, store):
        self.store = store

    def start(self, **values):
        try:
            return self.store.insert_jev(**values)
        except Exception as exc:
            log.error("Jev accounting unavailable: %s", type(exc).__name__)
            return None

    def update(self, call_id, **values):
        if call_id:
            try:
                self.store.update_jev(call_id, **values)
            except Exception as exc:
                log.error("Jev accounting update unavailable: %s", type(exc).__name__)

    def response(self, call_id, response, endpoint, duration_ms):
        try:
            body = response.text
            try:
                data = response.json()
                data = data if isinstance(data, dict) else {}
            except (ValueError, UnicodeError):
                data = {}
            self.update(
                call_id,
                http_status=response.status_code,
                duration_ms=duration_ms,
                response_bytes=len(response.content),
                response_text=redact(body[:RESPONSE_LIMIT]),
                response_truncated=int(len(body) > RESPONSE_LIMIT),
                provider_request_id=response.headers.get("x-request-id"),
                usage_json=json.dumps(data["usage"])
                if isinstance(data.get("usage"), dict)
                else None,
                **usage_values(data, endpoint),
            )
        except Exception as exc:
            log.error("Jev response accounting unavailable: %s", type(exc).__name__)
