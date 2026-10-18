import json
import math
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .jev_usage import usd


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=3000;
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, session_id TEXT NOT NULL,
                transport TEXT NOT NULL, endpoint TEXT NOT NULL, requested_model TEXT,
                reported_model TEXT, reasoning_effort TEXT, prompt_preview TEXT,
                request_bytes INTEGER, delivery_status TEXT DEFAULT 'forwarding',
                status_code INTEGER, latency_ms REAL, first_byte_ms REAL,
                input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
                classification_status TEXT DEFAULT 'pending', recommended_model TEXT,
                category TEXT, complexity TEXT, confidence REAL, mismatch INTEGER,
                uncertain INTEGER, classification_ms REAL, policy_version TEXT,
                classifier_model TEXT, probabilities TEXT, rationale TEXT,
                classification_error TEXT, upstream_error TEXT,
                context_truncated INTEGER, context_missing INTEGER
            );
            CREATE INDEX IF NOT EXISTS requests_created ON requests(created_at DESC);
            CREATE INDEX IF NOT EXISTS requests_session ON requests(session_id);
            CREATE INDEX IF NOT EXISTS requests_models ON requests(requested_model, recommended_model);
            CREATE TABLE IF NOT EXISTS jev_calls (
                id TEXT PRIMARY KEY, request_id TEXT NOT NULL, created_at TEXT NOT NULL,
                requested_model TEXT NOT NULL, reported_model TEXT, status TEXT DEFAULT 'pending',
                http_status INTEGER, duration_ms REAL, queue_ms REAL, question_count INTEGER,
                request_bytes INTEGER, response_bytes INTEGER, input_tokens INTEGER,
                output_tokens INTEGER, cost_nusd INTEGER, input_rate_nusd INTEGER,
                output_rate_nusd INTEGER, pricing_source TEXT, pricing_verified_at TEXT,
                response_text TEXT, response_truncated INTEGER DEFAULT 0,
                usage_json TEXT, provider_request_id TEXT, error_type TEXT
            );
            CREATE INDEX IF NOT EXISTS jev_calls_request ON jev_calls(request_id);
            CREATE INDEX IF NOT EXISTS jev_calls_created ON jev_calls(created_at DESC);
            UPDATE jev_calls SET status='interrupted', error_type='Observer restarted before call completed'
                WHERE status='pending';
            UPDATE requests SET classification_status='interrupted',
                classification_error='Observer restarted before classification completed'
                WHERE classification_status='pending';
            UPDATE requests SET delivery_status='interrupted'
                WHERE delivery_status IN ('forwarding', 'streaming');
        """)
        self.columns = {r[1] for r in self.conn.execute("PRAGMA table_info(requests)")}
        self.jev_columns = {r[1] for r in self.conn.execute("PRAGMA table_info(jev_calls)")}

    def close(self):
        self.conn.close()

    def insert(self, **values) -> str:
        values = {"id": uuid4().hex, "created_at": now(), **values}
        self._validate(values)
        with self.lock, self.conn:
            self.conn.execute(
                f"INSERT INTO requests ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                list(values.values()),
            )
        return values["id"]

    def update(self, request_id: str, **values):
        self._validate(values)
        if not values:
            return
        with self.lock, self.conn:
            self.conn.execute(
                f"UPDATE requests SET {','.join(k + '=?' for k in values)} WHERE id=?",
                [*values.values(), request_id],
            )

    def _validate(self, values):
        if not set(values) <= self.columns:
            raise ValueError("Unknown request column")

    def get(self, request_id: str) -> dict | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row):
        result = dict(row)
        if result.get("probabilities"):
            result["probabilities"] = json.loads(result["probabilities"])
        return result

    def query(self, filters: dict, limit=50, offset=0) -> dict:
        where, params = self._where(filters)
        with self.lock:
            rows = self.conn.execute(
                f"SELECT * FROM requests {where} ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            summary = dict(
                self.conn.execute(
                    f"""
                SELECT COUNT(*) AS total,
                    MAX(created_at) AS latest_request_at,
                    COALESCE(SUM(classification_status='complete'),0) AS classified,
                    COALESCE(SUM(mismatch=1),0) AS mismatches,
                    COALESCE(SUM(uncertain=1),0) AS uncertain,
                    COALESCE(SUM(classification_status='pending'),0) AS pending,
                    COALESCE(SUM(classification_status IN ('error','interrupted','skipped')),0) AS classification_errors,
                    AVG(latency_ms) AS avg_latency_ms,
                    COALESCE(SUM(input_tokens),0) AS input_tokens,
                    COALESCE(SUM(output_tokens),0) AS output_tokens
                FROM requests {where}
            """,
                    params,
                ).fetchone()
            )
            matrix = [
                dict(r)
                for r in self.conn.execute(
                    f"""
                SELECT requested_model, recommended_model, COUNT(*) AS count
                FROM requests {where + (" AND " if where else " WHERE ")} classification_status='complete'
                GROUP BY requested_model, recommended_model ORDER BY count DESC
            """,
                    params,
                )
            ]
        return {
            "items": [self._row(r) for r in rows],
            "summary": summary,
            "matrix": matrix,
            "limit": limit,
            "offset": offset,
        }

    @staticmethod
    def _where(filters):
        clauses, params = [], []
        for key in (
            "requested_model",
            "recommended_model",
            "category",
            "classification_status",
            "session_id",
            "transport",
            "delivery_status",
        ):
            if filters.get(key):
                clauses.append(f"{key}=?")
                params.append(filters[key])
        if filters.get("mismatch") is not None:
            clauses.append("mismatch=?")
            params.append(int(filters["mismatch"]))
        if filters.get("min_confidence") is not None:
            clauses.append("confidence>=?")
            params.append(filters["min_confidence"])
        if filters.get("q"):
            clauses.append("(prompt_preview LIKE ? ESCAPE '\\' OR session_id LIKE ? ESCAPE '\\')")
            escaped = filters["q"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%"] * 2)
        for key, op in (("after", ">="), ("before", "<=")):
            if filters.get(key):
                clauses.append(f"created_at{op}?")
                params.append(filters[key])
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), params

    def facets(self):
        with self.lock:
            result = {}
            for col in ("requested_model", "recommended_model", "category", "session_id"):
                result[col] = [
                    r[0]
                    for r in self.conn.execute(
                        f"SELECT DISTINCT {col} FROM requests WHERE {col} IS NOT NULL ORDER BY {col} LIMIT 200"
                    )
                ]
            return result

    def insert_jev(self, **values):
        values = {"id": uuid4().hex, "created_at": now(), **values}
        if not set(values) <= self.jev_columns:
            raise ValueError("Unknown Jev call column")
        with self.lock, self.conn:
            self.conn.execute(
                f"INSERT INTO jev_calls ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                list(values.values()),
            )
        return values["id"]

    def update_jev(self, call_id, **values):
        if not set(values) <= self.jev_columns:
            raise ValueError("Unknown Jev call column")
        if not values:
            return
        with self.lock, self.conn:
            self.conn.execute(
                f"UPDATE jev_calls SET {','.join(k + '=?' for k in values)} WHERE id=?",
                [*values.values(), call_id],
            )

    @staticmethod
    def _jev_row(row):
        result = dict(row)
        result["cost_usd"] = usd(result.get("cost_nusd"))
        return result

    def get_jev(self, call_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM jev_calls WHERE id=?", (call_id,)).fetchone()
        return self._jev_row(row) if row else None

    def query_jev(self, status=None, after=None, before=None, limit=20, offset=0):
        clauses, params = [], []
        if status:
            clauses.append("c.status=?")
            params.append(status)
        for value, operator in ((after, ">="), (before, "<=")):
            if value:
                clauses.append(f"c.created_at{operator}?")
                params.append(value)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.lock:
            # Deliberately exclude bodies from the polling/list endpoint. A response
            # is loaded locally only when the user expands its disclosure.
            cols = [
                f"c.{col}" for col in self.jev_columns if col not in {"response_text", "usage_json"}
            ]
            rows = self.conn.execute(
                f"SELECT {','.join(cols)}, r.session_id, r.requested_model AS codex_model "
                f"FROM jev_calls c LEFT JOIN requests r ON r.id=c.request_id {where} "
                "ORDER BY c.created_at DESC, c.rowid DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            summary = dict(
                self.conn.execute(
                    f"""
                SELECT COUNT(*) AS total,
                    COALESCE(SUM(status='success'),0) AS succeeded,
                    COALESCE(SUM(status='error'),0) AS errors,
                    COALESCE(SUM(status='pending'),0) AS pending,
                    COALESCE(SUM(status='interrupted'),0) AS interrupted,
                    COUNT(cost_nusd) AS priced,
                    COALESCE(SUM(cost_nusd),0) AS cost_nusd,
                    COUNT(input_tokens) AS usage_reported,
                    COALESCE(SUM(input_tokens),0) AS input_tokens,
                    COALESCE(SUM(output_tokens),0) AS output_tokens,
                    AVG(duration_ms) AS avg_duration_ms,
                    COUNT(duration_ms) AS timed,
                    AVG(queue_ms) AS avg_queue_ms,
                    COALESCE(SUM(response_bytes),0) AS response_bytes,
                    COALESCE(SUM(question_count),0) AS questions
                FROM jev_calls c {where}
            """,
                    params,
                ).fetchone()
            )
            percentile = self.conn.execute(
                f"SELECT duration_ms FROM jev_calls c {where + (' AND ' if where else ' WHERE ')} "
                "duration_ms IS NOT NULL ORDER BY duration_ms LIMIT 1 OFFSET ?",
                [*params, max(0, math.ceil(summary["timed"] * 0.95) - 1)],
            ).fetchone()
            legacy = self.conn.execute("""
                SELECT COUNT(*) FROM requests r
                WHERE r.classification_status IN ('complete','error','interrupted')
                AND NOT EXISTS(SELECT 1 FROM jev_calls c WHERE c.request_id=r.id)
            """).fetchone()[0]
        summary["cost_usd"] = usd(summary["cost_nusd"]) if summary["priced"] else None
        summary["unpriced"] = summary["total"] - summary["priced"]
        summary["p95_duration_ms"] = percentile[0] if percentile else None
        summary["legacy_classifications"] = legacy
        return {
            "items": [self._jev_row(r) for r in rows],
            "summary": summary,
            "limit": limit,
            "offset": offset,
        }
