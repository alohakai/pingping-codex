from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ChatBinding:
    chat_id: str
    master_thread_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    chat_id: str
    master_thread_id: str
    worker_thread_id: str
    turn_id: str | None
    title: str
    prompt: str
    status: str
    result: str
    created_at: str
    updated_at: str


class StateStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS chats (
                    chat_id TEXT PRIMARY KEY,
                    master_thread_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS processed_events (
                    event_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    master_thread_id TEXT NOT NULL,
                    worker_thread_id TEXT NOT NULL UNIQUE,
                    turn_id TEXT,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_chat_updated
                    ON tasks(chat_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_tasks_master
                    ON tasks(master_thread_id);
                """
            )

    def mark_event_once(self, event_id: str) -> bool:
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO processed_events(event_id, created_at) VALUES (?, ?)",
                    (event_id, _now()),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def bind_chat(self, chat_id: str, master_thread_id: str) -> ChatBinding:
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO chats(chat_id, master_thread_id, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    master_thread_id=excluded.master_thread_id,
                    updated_at=excluded.updated_at
                """,
                (chat_id, master_thread_id, now, now),
            )
        binding = self.get_chat(chat_id)
        assert binding is not None
        return binding

    def get_chat(self, chat_id: str) -> ChatBinding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM chats WHERE chat_id=?", (chat_id,)
            ).fetchone()
        return _chat_from_row(row) if row else None

    def get_chat_by_master(self, master_thread_id: str) -> ChatBinding | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM chats WHERE master_thread_id=?", (master_thread_id,)
            ).fetchone()
        return _chat_from_row(row) if row else None

    def reset_chat(self, chat_id: str) -> str | None:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT master_thread_id FROM chats WHERE chat_id=?", (chat_id,)
            ).fetchone()
            self._conn.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
        return str(row["master_thread_id"]) if row else None

    def create_task(
        self,
        *,
        chat_id: str,
        master_thread_id: str,
        worker_thread_id: str,
        prompt: str,
        title: str | None = None,
    ) -> TaskRecord:
        existing = self.get_task_by_worker(worker_thread_id)
        if existing:
            return existing
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        clean_title = title or _title_from_prompt(prompt)
        now = _now()
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO tasks(
                    task_id, chat_id, master_thread_id, worker_thread_id, turn_id,
                    title, prompt, status, result, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'running', '', ?, ?)
                """,
                (
                    task_id,
                    chat_id,
                    master_thread_id,
                    worker_thread_id,
                    clean_title,
                    prompt,
                    now,
                    now,
                ),
            )
        task = self.get_task(task_id)
        assert task is not None
        return task

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return _task_from_row(row) if row else None

    def get_task_by_worker(self, worker_thread_id: str) -> TaskRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tasks WHERE worker_thread_id=?", (worker_thread_id,)
            ).fetchone()
        return _task_from_row(row) if row else None

    def find_task_for_thread(self, thread_id: str) -> TaskRecord | None:
        task = self.get_task_by_worker(thread_id)
        if task:
            return task
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM tasks
                WHERE master_thread_id=? AND status IN ('pending', 'running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        return _task_from_row(row) if row else None

    def list_tasks(self, chat_id: str, limit: int = 10) -> list[TaskRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE chat_id=? ORDER BY updated_at DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [_task_from_row(row) for row in rows]

    def update_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        turn_id: str | None = None,
        result: str | None = None,
    ) -> TaskRecord | None:
        updates: dict[str, Any] = {"updated_at": _now()}
        if status is not None:
            updates["status"] = status
        if turn_id is not None:
            updates["turn_id"] = turn_id
        if result is not None:
            updates["result"] = result
        columns = ", ".join(f"{name}=?" for name in updates)
        values = list(updates.values()) + [task_id]
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE tasks SET {columns} WHERE task_id=?", values)
        return self.get_task(task_id)

    def export_summary(self) -> dict[str, Any]:
        with self._lock:
            chats = self._conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()[
                "n"
            ]
            tasks = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
        return {
            "chats": chats,
            "tasks": {str(row["status"]): int(row["n"]) for row in tasks},
            "database": str(self.path),
        }


def _chat_from_row(row: sqlite3.Row) -> ChatBinding:
    return ChatBinding(
        chat_id=str(row["chat_id"]),
        master_thread_id=str(row["master_thread_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _task_from_row(row: sqlite3.Row) -> TaskRecord:
    return TaskRecord(
        task_id=str(row["task_id"]),
        chat_id=str(row["chat_id"]),
        master_thread_id=str(row["master_thread_id"]),
        worker_thread_id=str(row["worker_thread_id"]),
        turn_id=str(row["turn_id"]) if row["turn_id"] else None,
        title=str(row["title"]),
        prompt=str(row["prompt"]),
        status=str(row["status"]),
        result=str(row["result"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _title_from_prompt(prompt: str) -> str:
    text = " ".join(prompt.strip().split())
    if not text:
        return "未命名任务"
    return text[:48] + ("…" if len(text) > 48 else "")
