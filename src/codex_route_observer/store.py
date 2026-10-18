import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


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
            UPDATE requests SET classification_status='interrupted',
                classification_error='Observer restarted before classification completed'
                WHERE classification_status='pending';
            UPDATE requests SET delivery_status='interrupted'
                WHERE delivery_status IN ('forwarding', 'streaming');
        """)
        self.columns = {r[1] for r in self.conn.execute("PRAGMA table_info(requests)")}

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
