"""SQLite 数据层：玩家表、礼包码表、兑换记录关联表（去重核心）。"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

import config

_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            -- 玩家表：一个昵称一条记录
            CREATE TABLE IF NOT EXISTS players (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname    TEXT NOT NULL UNIQUE,   -- 游戏内昵称（= 官方接口 userId）
                note        TEXT DEFAULT '',        -- 备注（如区服说明）
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL
            );

            -- 礼包码表
            CREATE TABLE IF NOT EXISTS codes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                expires_at  TEXT,                   -- ISO 时间，可空
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL
            );

            -- 兑换记录关联表：防止重复兑换的核心。 (player_id, code_id) 唯一
            CREATE TABLE IF NOT EXISTS redemptions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                player_id   INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
                code_id     INTEGER NOT NULL REFERENCES codes(id)   ON DELETE CASCADE,
                status      TEXT NOT NULL DEFAULT 'pending',  -- pending/success/already/invalid/...
                message     TEXT DEFAULT '',
                attempts    INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL,
                UNIQUE(player_id, code_id)
            );

            CREATE INDEX IF NOT EXISTS idx_redemptions_status ON redemptions(status);
            """
        )
        # 兼容旧库：补充 source 列（记录礼包码来源：manual / auto:xxx）
        try:
            conn.execute("ALTER TABLE codes ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass


# ---------------- 玩家 ----------------
def add_player(nickname: str, note: str = "") -> dict:
    nickname = nickname.strip()
    if not nickname:
        raise ValueError("昵称不能为空")
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO players(nickname, note, active, created_at) VALUES(?,?,1,?) "
            "ON CONFLICT(nickname) DO UPDATE SET active=1, note=excluded.note "
            "RETURNING *",
            (nickname, note, _now()),
        )
        return dict(cur.fetchone())


def list_players(active_only: bool = True) -> list[dict]:
    q = "SELECT * FROM players"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY id"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q).fetchall()]


def deactivate_player(nickname: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute("UPDATE players SET active=0 WHERE nickname=?", (nickname.strip(),))


# ---------------- 礼包码 ----------------
def add_code(code: str, description: str = "", expires_at: str | None = None,
             source: str = "manual") -> dict:
    code = code.strip()
    if not code:
        raise ValueError("礼包码不能为空")
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO codes(code, description, expires_at, active, source, created_at) "
            "VALUES(?,?,?,1,?,?) "
            "ON CONFLICT(code) DO UPDATE SET active=1, description=excluded.description, "
            "expires_at=excluded.expires_at, source=excluded.source RETURNING *",
            (code, description, expires_at, source, _now()),
        )
        return dict(cur.fetchone())


def add_code_and_enqueue(code: str, description: str = "", expires_at: str | None = None,
                         source: str = "manual") -> tuple[dict, int]:
    """录入礼包码并立即为所有已绑定玩家生成 pending 任务。返回 (code行, 新建任务数)。"""
    row = add_code(code, description, expires_at, source)
    queued = enqueue_redemptions_for_code(row["id"])
    return row, queued


def deactivate_code(code: str) -> bool:
    """管理员去激活某个礼包码（如自动抓到失效/无效码）。"""
    with _lock, get_conn() as conn:
        cur = conn.execute("UPDATE codes SET active=0 WHERE code=?", (code.strip(),))
        return cur.rowcount > 0


def list_codes(active_only: bool = False) -> list[dict]:
    q = "SELECT * FROM codes"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY id DESC"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q).fetchall()]


# ---------------- 兑换记录 ----------------
def enqueue_redemptions_for_code(code_id: int) -> int:
    """为某礼包码，给所有激活玩家创建 pending 记录（已存在的跳过）。返回新建条数。"""
    with _lock, get_conn() as conn:
        players = conn.execute("SELECT id FROM players WHERE active=1").fetchall()
        n = 0
        for p in players:
            cur = conn.execute(
                "INSERT OR IGNORE INTO redemptions(player_id, code_id, status, updated_at) "
                "VALUES(?,?,'pending',?)",
                (p["id"], code_id, _now()),
            )
            n += cur.rowcount
        return n


def enqueue_all_codes_for_player(player_id: int) -> int:
    """新玩家绑定时，把所有激活礼包码补齐为 pending（自动补发历史码）。"""
    with _lock, get_conn() as conn:
        codes = conn.execute("SELECT id FROM codes WHERE active=1").fetchall()
        n = 0
        for c in codes:
            cur = conn.execute(
                "INSERT OR IGNORE INTO redemptions(player_id, code_id, status, updated_at) "
                "VALUES(?,?,'pending',?)",
                (player_id, c["id"], _now()),
            )
            n += cur.rowcount
        return n


def get_pending_jobs() -> list[dict]:
    """取出所有待处理任务，联表带出昵称与码。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id AS redemption_id, r.player_id, r.code_id, r.attempts,
                   p.nickname, c.code
            FROM redemptions r
            JOIN players p ON p.id = r.player_id
            JOIN codes   c ON c.id = r.code_id
            WHERE r.status = 'pending' AND p.active = 1 AND c.active = 1
            ORDER BY r.id
            """
        ).fetchall()
        return [dict(r) for r in rows]


def update_redemption(redemption_id: int, status: str, message: str, inc_attempt: bool = True) -> None:
    with _lock, get_conn() as conn:
        if inc_attempt:
            conn.execute(
                "UPDATE redemptions SET status=?, message=?, attempts=attempts+1, updated_at=? WHERE id=?",
                (status, message, _now(), redemption_id),
            )
        else:
            conn.execute(
                "UPDATE redemptions SET status=?, message=?, updated_at=? WHERE id=?",
                (status, message, _now(), redemption_id),
            )


def stats() -> dict:
    with get_conn() as conn:
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM redemptions GROUP BY status"
            ).fetchall()
        }
        players = conn.execute("SELECT COUNT(*) AS n FROM players WHERE active=1").fetchone()["n"]
        codes = conn.execute("SELECT COUNT(*) AS n FROM codes WHERE active=1").fetchone()["n"]
        return {"players": players, "codes": codes, "redemptions_by_status": by_status}


def recent_redemptions(limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id, p.nickname, c.code, r.status, r.message, r.attempts, r.updated_at
            FROM redemptions r
            JOIN players p ON p.id = r.player_id
            JOIN codes   c ON c.id = r.code_id
            ORDER BY r.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
