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

                    CREATE TABLE IF NOT EXISTS image_assets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER,
                        memory_key TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        path TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'cached',
                        analysis_summary_json TEXT NOT NULL DEFAULT '{}',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        expires_at DATETIME NOT NULL,
                        deleted_at DATETIME
                    );
                    CREATE INDEX IF NOT EXISTS idx_image_assets_run ON image_assets(run_id, id);
                    CREATE INDEX IF NOT EXISTS idx_image_assets_memory ON image_assets(memory_key, created_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_image_assets_expiry ON image_assets(status, expires_at);

                    CREATE TABLE IF NOT EXISTS user_profiles (
                        memory_key TEXT PRIMARY KEY,
                        profile_json TEXT NOT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS connected_accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_key TEXT NOT NULL,
                        service_name TEXT NOT NULL,
                        login_identifier TEXT NOT NULL,
                        display_name TEXT NOT NULL DEFAULT '',
                        oauth_provider TEXT NOT NULL DEFAULT '',
                        session_available INTEGER NOT NULL DEFAULT 0,
                        last_verified_at DATETIME,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(memory_key, service_name, login_identifier)
                    );
                    CREATE INDEX IF NOT EXISTS idx_connected_accounts_memory ON connected_accounts(memory_key, service_name);

                    CREATE TABLE IF NOT EXISTS oauth_states (
                        state_token TEXT PRIMARY KEY,
                        memory_key TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        run_id INTEGER,
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        resolved_at DATETIME
                    );

                    CREATE TABLE IF NOT EXISTS memory_change_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        memory_key TEXT NOT NULL,
                        change_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS automation_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id INTEGER NOT NULL,
                        memory_key TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        intent TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    );

                    CREATE TABLE IF NOT EXISTS sensitive_checkpoints (
                        token TEXT PRIMARY KEY,
                        run_id INTEGER NOT NULL,
                        memory_key TEXT NOT NULL,
                        checkpoint_type TEXT NOT NULL,
                        prompt_text TEXT NOT NULL,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        status TEXT NOT NULL DEFAULT 'pending',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        resolved_at DATETIME
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
                    conn.execute(
                        """
                        UPDATE image_assets
                        SET status = CASE WHEN status = 'deleted' THEN status ELSE 'expired' END
                        WHERE run_id = ? AND status NOT IN ('deleted', 'missing')
                        """,
                        (run_id,),
                    )
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
                conn.execute("DELETE FROM connected_accounts WHERE memory_key = ?", (memory_key,))
                conn.execute("DELETE FROM oauth_states WHERE memory_key = ?", (memory_key,))
                conn.execute("DELETE FROM memory_change_log WHERE memory_key = ?", (memory_key,))
                conn.execute("DELETE FROM automation_runs WHERE memory_key = ?", (memory_key,))
                conn.execute("DELETE FROM sensitive_checkpoints WHERE memory_key = ?", (memory_key,))
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

    def remove_profile_fields(self, memory_key: str, fields: List[str]) -> Dict[str, Any]:
        profile = self.get_profile(memory_key)
        for field in fields:
            profile.pop(field, None)
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

    def log_memory_change(self, memory_key: str, change_type: str, payload: Dict[str, Any]) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO memory_change_log(memory_key, change_type, payload_json)
                    VALUES (?, ?, ?)
                    """,
                    (memory_key, change_type, json.dumps(payload, ensure_ascii=False)),
                )
                conn.commit()

    def upsert_connected_account(
        self,
        memory_key: str,
        *,
        service_name: str,
        login_identifier: str,
        display_name: str = "",
        oauth_provider: str = "",
        session_available: bool = False,
        last_verified_at: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO connected_accounts(
                        memory_key, service_name, login_identifier, display_name, oauth_provider,
                        session_available, last_verified_at, metadata_json, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(memory_key, service_name, login_identifier) DO UPDATE SET
                        display_name = excluded.display_name,
                        oauth_provider = excluded.oauth_provider,
                        session_available = excluded.session_available,
                        last_verified_at = excluded.last_verified_at,
                        metadata_json = excluded.metadata_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        memory_key,
                        service_name,
                        login_identifier,
                        display_name,
                        oauth_provider,
                        1 if session_available else 0,
                        last_verified_at or datetime.utcnow().isoformat(),
                        json.dumps(metadata or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()

    def get_connected_accounts(self, memory_key: str, service_name: Optional[str] = None) -> List[sqlite3.Row]:
        query = "SELECT * FROM connected_accounts WHERE memory_key = ?"
        params: List[Any] = [memory_key]
        if service_name:
            query += " AND service_name = ?"
            params.append(service_name)
        query += " ORDER BY updated_at DESC, id DESC"
        with self.lock:
            with self.connect() as conn:
                return conn.execute(query, tuple(params)).fetchall()

    def get_primary_connected_account(self, memory_key: str, service_name: str) -> Optional[sqlite3.Row]:
        rows = self.get_connected_accounts(memory_key, service_name=service_name)
        return rows[0] if rows else None

    def forget_connected_account(
        self,
        memory_key: str,
        *,
        service_name: str,
        login_identifier: Optional[str] = None,
    ) -> int:
        query = "DELETE FROM connected_accounts WHERE memory_key = ? AND service_name = ?"
        params: List[Any] = [memory_key, service_name]
        if login_identifier:
            query += " AND login_identifier = ?"
            params.append(login_identifier)
        with self.lock:
            with self.connect() as conn:
                cursor = conn.execute(query, tuple(params))
                conn.commit()
                return int(cursor.rowcount or 0)

    def create_oauth_state(
        self,
        *,
        state_token: str,
        memory_key: str,
        provider: str,
        run_id: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO oauth_states(state_token, memory_key, provider, run_id, metadata_json, status)
                    VALUES (?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        state_token,
                        memory_key,
                        provider,
                        run_id,
                        json.dumps(metadata or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()

    def get_oauth_state(self, state_token: str) -> Optional[sqlite3.Row]:
        with self.lock:
            with self.connect() as conn:
                return conn.execute(
                    "SELECT * FROM oauth_states WHERE state_token = ? LIMIT 1",
                    (state_token,),
                ).fetchone()

    def resolve_oauth_state(self, state_token: str) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE oauth_states
                    SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP
                    WHERE state_token = ?
                    """,
                    (state_token,),
                )
                conn.commit()

    def create_automation_run(
        self,
        *,
        run_id: int,
        memory_key: str,
        domain: str,
        intent: str,
        status: str,
        request_payload: Dict[str, Any],
    ) -> int:
        with self.lock:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO automation_runs(run_id, memory_key, domain, intent, status, request_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        memory_key,
                        domain,
                        intent,
                        status,
                        json.dumps(request_payload, ensure_ascii=False),
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid)

    def update_automation_run(self, automation_id: int, *, status: str, result_payload: Optional[Dict[str, Any]] = None) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE automation_runs
                    SET status = ?, result_json = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (status, json.dumps(result_payload or {}, ensure_ascii=False), automation_id),
                )
                conn.commit()

    def get_automation_run(self, automation_id: int) -> Optional[sqlite3.Row]:
        with self.lock:
            with self.connect() as conn:
                return conn.execute(
                    "SELECT * FROM automation_runs WHERE id = ? LIMIT 1",
                    (automation_id,),
                ).fetchone()

    def create_sensitive_checkpoint(
        self,
        *,
        token: str,
        run_id: int,
        memory_key: str,
        checkpoint_type: str,
        prompt_text: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sensitive_checkpoints(token, run_id, memory_key, checkpoint_type, prompt_text, payload_json, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending')
                    """,
                    (
                        token,
                        run_id,
                        memory_key,
                        checkpoint_type,
                        prompt_text,
                        json.dumps(payload or {}, ensure_ascii=False),
                    ),
                )
                conn.commit()

    def get_sensitive_checkpoint(self, token: str) -> Optional[sqlite3.Row]:
        with self.lock:
            with self.connect() as conn:
                return conn.execute(
                    "SELECT * FROM sensitive_checkpoints WHERE token = ? LIMIT 1",
                    (token,),
                ).fetchone()

    def resolve_sensitive_checkpoint(self, token: str, status: str) -> None:
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    """
                    UPDATE sensitive_checkpoints
                    SET status = ?, resolved_at = CURRENT_TIMESTAMP
                    WHERE token = ?
                    """,
                    (status, token),
                )
                conn.commit()

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

    def create_image_asset(
        self,
        *,
        memory_key: str,
        message_id: str,
        sha256: str,
        mime_type: str,
        size_bytes: int,
        path: str,
        expires_at: str,
        run_id: Optional[int] = None,
    ) -> int:
        with self.lock:
            with self.connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO image_assets(
                        run_id, memory_key, message_id, sha256, mime_type, size_bytes, path, status, expires_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'cached', ?)
                    """,
                    (run_id, memory_key, message_id, sha256, mime_type, size_bytes, path, expires_at),
                )
                conn.commit()
                return int(cursor.lastrowid)

    def attach_image_assets_to_run(self, run_id: int, image_asset_ids: List[int]) -> None:
        if not image_asset_ids:
            return
        placeholders = ",".join("?" for _ in image_asset_ids)
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    f"UPDATE image_assets SET run_id = ? WHERE id IN ({placeholders})",
                    (run_id, *image_asset_ids),
                )
                conn.commit()

    def get_image_assets_for_run(self, run_id: int) -> List[sqlite3.Row]:
        with self.lock:
            with self.connect() as conn:
                return conn.execute(
                    "SELECT * FROM image_assets WHERE run_id = ? ORDER BY id ASC",
                    (run_id,),
                ).fetchall()

    def update_image_asset_status(
        self,
        image_asset_id: int,
        *,
        status: str,
        analysis_summary: Optional[Dict[str, Any]] = None,
        deleted_at: Optional[str] = None,
    ) -> None:
        fields = ["status = ?"]
        params: List[Any] = [status]
        if analysis_summary is not None:
            fields.append("analysis_summary_json = ?")
            params.append(json.dumps(analysis_summary, ensure_ascii=False))
        if deleted_at is not None:
            fields.append("deleted_at = ?")
            params.append(deleted_at)
        params.append(image_asset_id)
        with self.lock:
            with self.connect() as conn:
                conn.execute(
                    f"UPDATE image_assets SET {', '.join(fields)} WHERE id = ?",
                    tuple(params),
                )
                conn.commit()

    def list_expired_image_assets(self) -> List[sqlite3.Row]:
        with self.lock:
            with self.connect() as conn:
                return conn.execute(
                    """
                    SELECT * FROM image_assets
                    WHERE status NOT IN ('deleted')
                      AND expires_at < CURRENT_TIMESTAMP
                    ORDER BY id ASC
                    """
                ).fetchall()

    def mark_image_asset_missing(self, image_asset_id: int) -> None:
        self.update_image_asset_status(
            image_asset_id,
            status="missing",
            deleted_at=datetime.utcnow().isoformat(),
        )

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

    def get_artifact_by_ref_key(self, ref_key: str) -> Optional[sqlite3.Row]:
        with self.lock:
            with self.connect() as conn:
                return conn.execute(
                    "SELECT * FROM artifacts WHERE ref_key = ? ORDER BY id DESC LIMIT 1",
                    (ref_key,),
                ).fetchone()

    def get_recent_memory_artifacts(
        self,
        memory_key: str,
        *,
        kinds: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[sqlite3.Row]:
        query = """
            SELECT a.*
            FROM artifacts a
            JOIN task_runs r ON r.id = a.run_id
            WHERE r.memory_key = ?
        """
        params: List[Any] = [memory_key]
        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            query += f" AND a.kind IN ({placeholders})"
            params.extend(kinds)
        query += " ORDER BY a.id DESC LIMIT ?"
        params.append(limit)
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
        accounts = []
        for row in self.get_connected_accounts(memory_key):
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            accounts.append(
                {
                    "service_name": row["service_name"],
                    "login_identifier": row["login_identifier"],
                    "display_name": row["display_name"],
                    "oauth_provider": row["oauth_provider"],
                    "session_available": bool(row["session_available"]),
                    "last_verified_at": row["last_verified_at"] or "",
                    "metadata": metadata,
                }
            )
        artifacts = [
            {
                "kind": row["kind"],
                "ref_key": row["ref_key"],
                "content": row["content"],
            }
            for row in self.get_artifacts(run_id)
        ]
        recent_service_artifacts = []
        for row in self.get_recent_memory_artifacts(
            memory_key,
            kinds=["google_task", "google_event"],
            limit=5,
        ):
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            recent_service_artifacts.append(
                {
                    "kind": row["kind"],
                    "ref_key": row["ref_key"],
                    "content": row["content"],
                    "metadata": metadata,
                }
            )
        image_assets = []
        for row in self.get_image_assets_for_run(run_id):
            try:
                analysis_summary = json.loads(row["analysis_summary_json"] or "{}")
            except json.JSONDecodeError:
                analysis_summary = {}
            image_assets.append(
                {
                    "id": row["id"],
                    "message_id": row["message_id"],
                    "sha256": row["sha256"],
                    "mime_type": row["mime_type"],
                    "size_bytes": row["size_bytes"],
                    "path": row["path"],
                    "status": row["status"],
                    "analysis_summary": analysis_summary,
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                }
            )
        return {
            "history": self.history_to_text(memory_key),
            "profile": self.get_profile(memory_key),
            "connected_accounts": accounts,
            "artifacts": artifacts,
            "image_assets": image_assets,
            "recent_service_artifacts": recent_service_artifacts,
            "latest_approval_response": approval["response_text"] if approval else "",
        }
