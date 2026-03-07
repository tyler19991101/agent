import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from secretary_agent.models import TaskRun


class SQLiteStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_key TEXT NOT NULL,
                        role TEXT NOT NULL,
                        text TEXT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_history_key_id ON conversation_history(memory_key, id);

                    CREATE TABLE IF NOT EXISTS bot_message_store (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        message_id TEXT NOT NULL UNIQUE,
                        text TEXT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_bot_msg_id ON bot_message_store(message_id);

                    CREATE TABLE IF NOT EXISTS task_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        external_event_id TEXT UNIQUE,
                        memory_key TEXT NOT NULL,
                        task_type TEXT NOT NULL DEFAULT 'information_request',
                        status TEXT NOT NULL,
                        user_goal TEXT NOT NULL,
                        normalized_goal TEXT NOT NULL,
                        requires_approval INTEGER NOT NULL DEFAULT 0,
                        current_phase TEXT NOT NULL,
                        source_payload_json TEXT,
                        error TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        started_at DATETIME,
                        finished_at DATETIME
                    );
                    CREATE INDEX IF NOT EXISTS idx_task_runs_status_created ON task_runs(status, created_at);
                    CREATE INDEX IF NOT EXISTS idx_task_runs_memory ON task_runs(memory_key, id);

                    CREATE TABLE IF NOT EXISTS task_steps (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        step_type TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        tool_name TEXT,
                        status TEXT NOT NULL,
                        input_json TEXT,
                        output_json TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_task_steps_run ON task_steps(run_id, id);

                    CREATE TABLE IF NOT EXISTS artifacts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        ref_key TEXT,
                        content TEXT NOT NULL,
                        metadata_json TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id, id);

                    CREATE TABLE IF NOT EXISTS user_profiles (
                        memory_key TEXT PRIMARY KEY,
                        profile_json TEXT NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS pending_approvals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        memory_key TEXT NOT NULL,
                        approval_type TEXT NOT NULL,
                        prompt_text TEXT NOT NULL,
                        prompt_message_id TEXT,
                        options_json TEXT,
                        status TEXT NOT NULL,
                        response_text TEXT,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        resolved_at DATETIME
                    );
                    CREATE INDEX IF NOT EXISTS idx_pending_approvals_memory_status ON pending_approvals(memory_key, status, id);
                    """
                )
                conn.commit()

    def append_history(self, memory_key: str, role: str, text: str, keep_latest: int = 12) -> None:
        if not text:
            return
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO conversation_history(memory_key, role, text) VALUES (?, ?, ?)",
                    (memory_key, role, text[:3500]),
                )
                conn.execute(
                    """
                    DELETE FROM conversation_history
                    WHERE memory_key = ?
                      AND id NOT IN (
                        SELECT id FROM conversation_history
                        WHERE memory_key = ?
                        ORDER BY id DESC
                        LIMIT ?
                      )
                    """,
                    (memory_key, memory_key, keep_latest),
                )
                conn.commit()

    def prune_short_context(self, memory_key: str, ttl_days: int) -> None:
        if ttl_days <= 0:
            return
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    DELETE FROM conversation_history
                    WHERE memory_key = ?
                      AND created_at < datetime('now', ?)
                    """,
                    (memory_key, f"-{ttl_days} days"),
                )
                stale_run_rows = conn.execute(
                    """
                    SELECT id FROM task_runs
                    WHERE memory_key = ?
                      AND created_at < datetime('now', ?)
                      AND status IN ('completed', 'failed')
                    """,
                    (memory_key, f"-{ttl_days} days"),
                ).fetchall()
                stale_run_ids = [int(row["id"]) for row in stale_run_rows]
                for run_id in stale_run_ids:
                    conn.execute("DELETE FROM task_steps WHERE run_id = ?", (run_id,))
                    conn.execute("DELETE FROM artifacts WHERE run_id = ?", (run_id,))
                    conn.execute("DELETE FROM pending_approvals WHERE run_id = ?", (run_id,))
                if stale_run_ids:
                    placeholders = ",".join("?" for _ in stale_run_ids)
                    conn.execute(
                        f"DELETE FROM task_runs WHERE id IN ({placeholders})",
                        tuple(stale_run_ids),
                    )

                stale_pending_rows = conn.execute(
                    """
                    SELECT id, run_id FROM pending_approvals
                    WHERE memory_key = ?
                      AND status = 'pending'
                      AND created_at < datetime('now', ?)
                    """,
                    (memory_key, f"-{ttl_days} days"),
                ).fetchall()
                stale_pending_run_ids = []
                for row in stale_pending_rows:
                    conn.execute(
                        """
                        UPDATE pending_approvals
                        SET status = 'cancelled', resolved_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (int(row["id"]),),
                    )
                    stale_pending_run_ids.append(int(row["run_id"]))
                for run_id in stale_pending_run_ids:
                    conn.execute(
                        """
                        UPDATE task_runs
                        SET status = 'failed',
                            current_phase = 'expired',
                            error = 'Short-term context expired',
                            finished_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                          AND status = 'waiting_approval'
                        """,
                        (run_id,),
                    )

                conn.execute(
                    """
                    DELETE FROM bot_message_store
                    WHERE created_at < datetime('now', ?)
                    """,
                    (f"-{ttl_days} days",),
                )
                conn.commit()

    def history_to_text(self, memory_key: str) -> str:
        with self.lock:
            with self.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT role, text FROM conversation_history
                    WHERE memory_key = ?
                    ORDER BY id ASC
                    """,
                    (memory_key,),
                ).fetchall()
        lines = []
        for row in rows:
            role_label = "使用者" if row["role"] == "user" else "秘書"
            lines.append(f"{role_label}: {row['text']}")
        return "\n".join(lines)

    def clear_memory(self, memory_key: str) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute("DELETE FROM conversation_history WHERE memory_key = ?", (memory_key,))
                conn.execute("DELETE FROM user_profiles WHERE memory_key = ?", (memory_key,))
                conn.execute(
                    "UPDATE pending_approvals SET status = 'cancelled', resolved_at = CURRENT_TIMESTAMP WHERE memory_key = ? AND status = 'pending'",
                    (memory_key,),
                )
                conn.commit()

    def get_profile(self, memory_key: str) -> Dict[str, Any]:
        with self.lock:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT profile_json FROM user_profiles WHERE memory_key = ?",
                    (memory_key,),
                ).fetchone()
        if not row:
            return {}
        try:
            return json.loads(row["profile_json"])
        except json.JSONDecodeError:
            return {}

    def update_profile(self, memory_key: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        profile = self.get_profile(memory_key)
        profile.update({k: v for k, v in updates.items() if v not in (None, "", [], {})})
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO user_profiles(memory_key, profile_json, updated_at)
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(memory_key) DO UPDATE SET
                        profile_json = excluded.profile_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (memory_key, json.dumps(profile, ensure_ascii=False)),
                )
                conn.commit()
        return profile

    def create_task_run(
        self,
        *,
        memory_key: str,
        user_goal: str,
        normalized_goal: str,
        source_payload: Dict[str, Any],
        external_event_id: Optional[str],
    ) -> Tuple[int, bool]:
        payload_json = json.dumps(source_payload, ensure_ascii=False)
        with self.lock:
            with self.connect() as conn:
                try:
                    cursor = conn.execute(
                        """
                        INSERT INTO task_runs(
                            external_event_id, memory_key, status, user_goal, normalized_goal, current_phase, source_payload_json
                        ) VALUES (?, ?, 'queued', ?, ?, 'new', ?)
                        """,
                        (external_event_id, memory_key, user_goal, normalized_goal, payload_json),
                    )
                    conn.commit()
                    return int(cursor.lastrowid), True
                except sqlite3.IntegrityError:
                    row = conn.execute(
                        "SELECT id FROM task_runs WHERE external_event_id = ?",
                        (external_event_id,),
                    ).fetchone()
                    return int(row["id"]), False

    def get_task_run(self, run_id: int) -> TaskRun:
        with self.lock:
            with self.connect() as conn:
                row = conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            raise KeyError(f"Task run not found: {run_id}")
        return TaskRun(
            id=row["id"],
            memory_key=row["memory_key"],
            task_type=row["task_type"],
            status=row["status"],
            user_goal=row["user_goal"],
            normalized_goal=row["normalized_goal"],
            requires_approval=bool(row["requires_approval"]),
            current_phase=row["current_phase"],
            error=row["error"],
        )

    def claim_next_run(self) -> Optional[TaskRun]:
        with self.lock:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    """
                    SELECT * FROM task_runs
                    WHERE status = 'queued'
                    ORDER BY id ASC
                    LIMIT 1
                    """
                ).fetchone()
                if not row:
                    conn.commit()
                    return None
                conn.execute(
                    """
                    UPDATE task_runs
                    SET status = 'processing', started_at = COALESCE(started_at, CURRENT_TIMESTAMP)
                    WHERE id = ? AND status = 'queued'
                    """,
                    (row["id"],),
                )
                conn.commit()
        return self.get_task_run(int(row["id"]))

    def update_run_status(
        self,
        run_id: int,
        *,
        status: str,
        task_type: Optional[str] = None,
        requires_approval: Optional[bool] = None,
        current_phase: Optional[str] = None,
        normalized_goal: Optional[str] = None,
        error: Optional[str] = None,
        finished: bool = False,
    ) -> None:
        fields = ["status = ?"]
        params: List[Any] = [status]
        if task_type is not None:
            fields.append("task_type = ?")
            params.append(task_type)
        if requires_approval is not None:
            fields.append("requires_approval = ?")
            params.append(1 if requires_approval else 0)
        if current_phase is not None:
            fields.append("current_phase = ?")
            params.append(current_phase)
        if normalized_goal is not None:
            fields.append("normalized_goal = ?")
            params.append(normalized_goal)
        if error is not None:
            fields.append("error = ?")
            params.append(error)
        if finished:
            fields.append("finished_at = CURRENT_TIMESTAMP")
        params.append(run_id)
        query = f"UPDATE task_runs SET {', '.join(fields)} WHERE id = ?"
        with self.lock:
            with self.connect() as conn:
                conn.execute(query, tuple(params))
                conn.commit()

    def add_step(
        self,
        run_id: int,
        *,
        step_type: str,
        actor: str,
        status: str,
        input_payload: Optional[Dict[str, Any]] = None,
        output_payload: Optional[Dict[str, Any]] = None,
        tool_name: Optional[str] = None,
    ) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO task_steps(run_id, step_type, actor, tool_name, status, input_json, output_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        step_type,
                        actor,
                        tool_name,
                        status,
                        json.dumps(input_payload or {}, ensure_ascii=False),
                        json.dumps(output_payload or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()

    def add_artifact(
        self,
        run_id: int,
        *,
        kind: str,
        content: str,
        ref_key: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO artifacts(run_id, kind, ref_key, content, metadata_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        kind,
                        ref_key,
                        content,
                        json.dumps(metadata or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()

    def get_artifacts(self, run_id: int, kind: Optional[str] = None) -> List[sqlite3.Row]:
        query = "SELECT * FROM artifacts WHERE run_id = ?"
        params: List[Any] = [run_id]
        if kind:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY id ASC"
        with self.lock:
            with self.connect() as conn:
                return conn.execute(query, tuple(params)).fetchall()

    def create_pending_approval(
        self,
        *,
        run_id: int,
        memory_key: str,
        approval_type: str,
        prompt_text: str,
        options: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        with self.lock:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO pending_approvals(run_id, memory_key, approval_type, prompt_text, options_json, status)
                    VALUES (?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        run_id,
                        memory_key,
                        approval_type,
                        prompt_text,
                        json.dumps(options or [], ensure_ascii=False),
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid)

    def get_open_approval(self, memory_key: str) -> Optional[sqlite3.Row]:
        with self.lock:
            with self.connect() as conn:
                return conn.execute(
                    """
                    SELECT * FROM pending_approvals
                    WHERE memory_key = ? AND status = 'pending'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (memory_key,),
                ).fetchone()

    def set_approval_prompt_message(self, approval_id: int, message_id: Optional[str]) -> None:
        if not message_id:
            return
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    "UPDATE pending_approvals SET prompt_message_id = ? WHERE id = ?",
                    (message_id, approval_id),
                )
                conn.commit()

    def resolve_approval(self, approval_id: int, response_text: str) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE pending_approvals
                    SET status = 'resolved', response_text = ?, resolved_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (response_text, approval_id),
                )
                conn.commit()

    def get_latest_resolved_approval(self, run_id: int) -> Optional[sqlite3.Row]:
        with self.lock:
            with self.connect() as conn:
                return conn.execute(
                    """
                    SELECT * FROM pending_approvals
                    WHERE run_id = ? AND status = 'resolved'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()

    def store_bot_messages(self, message_ids: List[str], text_chunks: List[str]) -> None:
        if not message_ids:
            return
        with self.lock:
            with self.connect() as conn:
                for message_id, text in zip(message_ids, text_chunks):
                    conn.execute(
                        """
                        INSERT INTO bot_message_store(message_id, text)
                        VALUES (?, ?)
                        ON CONFLICT(message_id) DO UPDATE SET text = excluded.text
                        """,
                        (message_id, text[:4500]),
                    )
                conn.commit()

    def get_bot_message_content(self, message_id: Optional[str]) -> str:
        if not message_id:
            return ""
        with self.lock:
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT text FROM bot_message_store WHERE message_id = ? LIMIT 1",
                    (message_id,),
                ).fetchone()
        return row["text"] if row else ""

    def build_runtime_context(self, run_id: int, memory_key: str) -> Dict[str, Any]:
        approval = self.get_latest_resolved_approval(run_id)
        artifacts = [
            {
                "kind": row["kind"],
                "ref_key": row["ref_key"],
                "content": row["content"],
            }
            for row in self.get_artifacts(run_id)
        ]
        return {
            "history": self.history_to_text(memory_key),
            "profile": self.get_profile(memory_key),
            "artifacts": artifacts,
            "latest_approval_response": approval["response_text"] if approval else "",
        }
