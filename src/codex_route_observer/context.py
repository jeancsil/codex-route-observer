"""Create bounded classifier context; the forwarded payload is never modified."""

import gzip
import json
import re
import zlib
from collections import OrderedDict

import zstandard

MAX_BODY = 32 * 1024 * 1024
MAX_CONTEXT = 24_000
SECRET = re.compile(
    r"(?i)\b(?:sk-[a-z0-9_-]{12,}|crsr_[a-z0-9_-]{12,}|Bearer\s+[a-z0-9._~+/-]{12,})"
)


def redact(text: str) -> str:
    return SECRET.sub("[redacted credential]", text)


def decode_payload(raw: bytes, encoding: str = "") -> dict | None:
    try:
        if encoding == "gzip":
            raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, MAX_BODY + 1)
        elif encoding == "zstd":
            with zstandard.ZstdDecompressor().stream_reader(raw) as reader:
                raw = reader.read(MAX_BODY + 1)
        elif encoding and encoding != "identity":
            return None
        if len(raw) > MAX_BODY:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ValueError, OSError, zstandard.ZstdError, gzip.BadGzipFile, zlib.error):
        return None


def text_content(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(text_content(item) for item in value)
    if isinstance(value, dict):
        if value.get("type") in {"input_image", "image_url"}:
            return "[image attached]"
        text = value.get("text", value.get("content", value.get("output", "")))
        return text_content(text)
    return ""


class ContextBuilder:
    def __init__(self):
        # Bounded and ephemeral: no full request bodies or authorization headers
        # are written to disk. Session keys come from Codex's request metadata.
        self.recent: OrderedDict[str, str] = OrderedDict()

    def build(self, payload: dict, session_id: str) -> dict:
        raw_input = payload.get("input", [])
        items = (
            [{"role": "user", "content": raw_input}] if isinstance(raw_input, str) else raw_input
        )
        if not isinstance(items, list):
            items = []
        prompt = ""
        context = []
        truncated = False
        for item in items:
            if not isinstance(item, dict):
                continue
            role = item.get("role", item.get("type", "unknown"))
            text = text_content(item)
            if role == "user" and text.strip():
                prompt = text
            if text:
                truncated |= len(text) > 3000
                context.append({"role": role, "text": redact(text[-3000:])})
        inherited = not bool(prompt)
        if prompt:
            self.recent[session_id] = redact(prompt[-6000:])
            self.recent.move_to_end(session_id)
        else:
            prompt = self.recent.get(session_id, "")
        while len(self.recent) > 256:
            self.recent.popitem(last=False)
        instructions = text_content(payload.get("instructions", ""))
        truncated |= len(items) > 8 or len(instructions) > 3000 or len(prompt) > 6000
        state = {
            "user_request": redact(prompt[-6000:]),
            "recent_context": context[-8:],
            "instructions": redact(instructions[:3000]),
            "has_tools": bool(payload.get("tools")),
            "has_images": "[image attached]" in json.dumps(context),
            "continuation": bool(payload.get("previous_response_id")) or inherited,
            "user_request_inherited": inherited and bool(prompt),
            "context_missing": not bool(prompt),
            "context_truncated": truncated,
        }
        while len(json.dumps(state)) > MAX_CONTEXT and state["recent_context"]:
            state["recent_context"].pop(0)
            state["context_truncated"] = True
        return state
