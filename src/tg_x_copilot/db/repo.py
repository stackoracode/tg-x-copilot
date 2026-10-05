"""Repository: all SQL lives here. Returns plain dicts with JSON columns decoded."""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..models import InputEnvelope, TaskStatus
from .pool import Database

_TASK_JSON = ("envelope", "jev_result", "evaluation", "draft_meta")
_MEDIA_JSON = ("analysis",)


def _dumps(v: Any) -> str | None:
    if v is None:
        return None
    if hasattr(v, "model_dump"):
        v = v.model_dump(mode="json")
    return json.dumps(v, ensure_ascii=False, default=str)


def _decode(row: dict[str, Any] | None, cols: tuple[str, ...]) -> dict[str, Any] | None:
    if row is None:
        return None
    for c in cols:
        if isinstance(row.get(c), (str, bytes)):
            row[c] = json.loads(row[c])
    return row


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ tasks

    async def create_task(self, env: InputEnvelope) -> str:
        task_id = uuid.uuid4().hex
        await self.db.execute(
            "INSERT INTO tasks (id, status, locale, market, tg_chat_id, tg_user_id, envelope,"
            " source_text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                task_id, TaskStatus.RECEIVED.value, env.locale, env.market, env.chat_id,
                env.user_id, _dumps(env), env.text,
            ),
        )
        return task_id

    async def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM tasks WHERE id=%s", (task_id,))
        return _decode(row, _TASK_JSON)

    async def list_tasks(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        cols = ("id, status, stage, locale, score, route, attempts, LEFT(source_text, 140) AS"
                " preview, LEFT(error, 200) AS error, created_at, updated_at")
        if status:
            rows = await self.db.fetchall(
                f"SELECT {cols} FROM tasks WHERE status=%s ORDER BY created_at DESC LIMIT %s",
                (status, limit),
            )
        else:
            rows = await self.db.fetchall(
                f"SELECT {cols} FROM tasks ORDER BY created_at DESC LIMIT %s", (limit,)
            )
        return rows

    async def status_counts(self) -> dict[str, int]:
        rows = await self.db.fetchall("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}

    async def recoverable_task_ids(self) -> list[str]:
        rows = await self.db.fetchall(
            "SELECT id FROM tasks WHERE status IN (%s,%s) ORDER BY created_at",
            (TaskStatus.RECEIVED.value, TaskStatus.PROCESSING.value),
        )
        return [r["id"] for r in rows]

    async def set_status(
        self, task_id: str, status: TaskStatus, *, stage: str | None = None,
        error: str | None = None,
    ) -> None:
        await self.db.execute(
            "UPDATE tasks SET status=%s, stage=%s, error=%s WHERE id=%s",
            (status.value, stage, error, task_id),
        )

    async def set_stage(self, task_id: str, stage: str) -> None:
        await self.db.execute("UPDATE tasks SET stage=%s WHERE id=%s", (stage, task_id))

    async def begin_attempt(self, task_id: str) -> None:
        await self.db.execute(
            "UPDATE tasks SET status=%s, stage='start', error=NULL, attempts=attempts+1"
            " WHERE id=%s",
            (TaskStatus.PROCESSING.value, task_id),
        )

    async def save_triage(self, task_id: str, triage: Any) -> None:
        await self.db.execute(
            "UPDATE tasks SET jev_result=%s, score=%s, route=%s WHERE id=%s",
            (_dumps(triage), triage.value, triage.route, task_id),
        )

    async def save_evaluation(self, task_id: str, evaluation: Any) -> None:
        await self.db.execute(
            "UPDATE tasks SET evaluation=%s WHERE id=%s", (_dumps(evaluation), task_id)
        )

    async def save_draft(
        self, task_id: str, status: TaskStatus, draft_text: str, draft_meta: dict[str, Any]
    ) -> None:
        await self.db.execute(
            "UPDATE tasks SET status=%s, stage='done', draft_text=%s, draft_meta=%s, error=NULL"
            " WHERE id=%s",
            (status.value, draft_text, _dumps(draft_meta), task_id),
        )

    # ------------------------------------------------------------------ media

    async def upsert_media(
        self, task_id: str, idx: int, *, message_id: int, kind: str, mime: str | None,
        size_bytes: int | None, width: int | None = None, height: int | None = None,
        source_sha256: str | None = None,
    ) -> None:
        """Describe an incoming media item (metadata only; the bytes are never stored here).
        Re-runs reset the per-run columns."""
        await self.db.execute(
            "INSERT INTO task_media (task_id, idx, tg_message_id, kind, mime, size_bytes, width,"
            " height, source_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE kind=VALUES(kind), mime=VALUES(mime),"
            " size_bytes=VALUES(size_bytes), width=VALUES(width), height=VALUES(height),"
            " source_sha256=VALUES(source_sha256), analysis=NULL, decision=NULL,"
            " decision_reason=NULL, ai_generated=0",
            (task_id, idx, message_id, kind, mime, size_bytes, width, height, source_sha256),
        )

    async def update_media_analysis(self, task_id: str, idx: int, analysis: Any) -> None:
        await self.db.execute(
            "UPDATE task_media SET analysis=%s WHERE task_id=%s AND idx=%s",
            (_dumps(analysis), task_id, idx),
        )

    async def update_media_decision(
        self, task_id: str, idx: int, decision: str, reason: str, *, ai_generated: bool = False
    ) -> None:
        await self.db.execute(
            "UPDATE task_media SET decision=%s, decision_reason=%s, ai_generated=%s"
            " WHERE task_id=%s AND idx=%s",
            (decision, reason, int(ai_generated), task_id, idx),
        )

    async def set_media_asset(self, task_id: str, idx: int, *, key: str, kind: str, size: int,
                              mime: str) -> None:
        await self.db.execute(
            "UPDATE task_media SET asset_key=%s, asset_kind=%s, asset_size=%s, asset_mime=%s"
            " WHERE task_id=%s AND idx=%s",
            (key, kind, size, mime, task_id, idx),
        )

    async def clear_media_asset(self, task_id: str, idx: int) -> None:
        await self.db.execute(
            "UPDATE task_media SET asset_key=NULL, asset_kind=NULL, asset_size=NULL,"
            " asset_mime=NULL WHERE task_id=%s AND idx=%s",
            (task_id, idx),
        )

    async def asset_key_referenced(self, key: str) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 AS x FROM task_media WHERE asset_key=%s LIMIT 1", (key,)
        )
        return row is not None

    async def stored_bytes(self) -> int:
        """Approximate R2 usage: sum of distinct persisted assets (dedup-aware)."""
        row = await self.db.fetchone(
            "SELECT COALESCE(SUM(sz), 0) AS total FROM (SELECT MAX(asset_size) AS sz"
            " FROM task_media WHERE asset_key IS NOT NULL GROUP BY asset_key) t"
        )
        return int(row["total"]) if row else 0

    async def previous_uses(self, source_sha256: str, exclude_task: str) -> list[dict[str, Any]]:
        """Other tasks that received the same source file (duplicate-forward detection)."""
        return await self.db.fetchall(
            "SELECT m.task_id, t.status FROM task_media m JOIN tasks t ON t.id = m.task_id"
            " WHERE m.source_sha256=%s AND m.task_id<>%s ORDER BY m.id DESC LIMIT 5",
            (source_sha256, exclude_task),
        )

    async def list_media(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM task_media WHERE task_id=%s ORDER BY idx", (task_id,)
        )
        return [_decode(r, _MEDIA_JSON) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------ events (debug timeline)

    async def add_event(
        self, task_id: str, step: str, message: str, *, level: str = "info",
        data: Any = None,
    ) -> None:
        await self.db.execute(
            "INSERT INTO task_events (task_id, level, step, message, data)"
            " VALUES (%s,%s,%s,%s,%s)",
            (task_id, level, step, message[:2000], _dumps(data)),
        )

    async def list_events(self, task_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM task_events WHERE task_id=%s ORDER BY id", (task_id,)
        )
        return [_decode(r, ("data",)) for r in rows]  # type: ignore[misc]

    # ------------------------------------------------------------------ settings

    async def get_settings(self) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT k, v FROM settings")
        return {r["k"]: json.loads(r["v"]) for r in rows}

    async def set_settings(self, values: dict[str, tuple[Any, bool]]) -> None:
        """values: key -> (value, is_secret)"""
        await self.db.executemany(
            "INSERT INTO settings (k, v, is_secret) VALUES (%s,%s,%s)"
            " ON DUPLICATE KEY UPDATE v=VALUES(v), is_secret=VALUES(is_secret)",
            [(k, json.dumps(v), int(sec)) for k, (v, sec) in values.items()],
        )

    async def delete_setting(self, key: str) -> None:
        await self.db.execute("DELETE FROM settings WHERE k=%s", (key,))

    # ------------------------------------------------------------------ rules / hooks / locales

    async def get_rules(self, locale: str, market: str) -> dict[str, Any]:
        rows = await self.db.fetchall(
            "SELECT rule_key, rule_value FROM x_rules WHERE enabled=1 AND locale=%s"
            " AND market=%s",
            (locale, market),
        )
        return {r["rule_key"]: json.loads(r["rule_value"]) for r in rows}

    async def get_hooks(self, locale: str, market: str, limit: int = 8) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            "SELECT name, pattern, example FROM hooks WHERE enabled=1 AND locale=%s"
            " AND market=%s ORDER BY weight DESC, id LIMIT %s",
            (locale, market, limit),
        )

    # ------------------------------------------------------------------ models

    async def upsert_models(self, provider: str, models: list[dict[str, Any]]) -> int:
        rows = [
            (provider, m["id"], m.get("owned_by"), json.dumps(m.get("capabilities", [])),
             json.dumps(m, default=str))
            for m in models if m.get("id")
        ]
        await self.db.executemany(
            "INSERT INTO models (provider, model_id, owned_by, capabilities, raw)"
            " VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE owned_by=VALUES(owned_by),"
            " capabilities=VALUES(capabilities), raw=VALUES(raw), last_seen_at=CURRENT_TIMESTAMP(3)",
            rows,
        )
        return len(rows)

    async def list_models(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT provider, model_id, owned_by, capabilities, last_seen_at FROM models"
            " ORDER BY provider, model_id"
        )
        return [_decode(r, ("capabilities",)) for r in rows]  # type: ignore[misc]
