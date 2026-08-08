"""SQLite 数据层：玩家表(UID 主键+7天有效期)、礼包码表(含奖励/过期/状态)、兑换记录关联表。"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import config

_lock = threading.Lock()


def _now() -> str:
    """返回 UTC 当前时间的 ISO 字符串（不含时区后缀，方便 SQLite 比较）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _expires(days: int = 7) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=days)).isoformat(
        timespec="seconds"
    )


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


def _migrate_players_v1(conn):
    """兼容旧库：原 players 表以 nickname 为主键，现改为 uid 主键 + 有效期。"""
    print("[db] 检测到旧版 players 表，执行迁移到 UID + 7 天有效期 schema")
    conn.execute(
        """
        CREATE TABLE players_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            uid         TEXT NOT NULL UNIQUE,
            nickname    TEXT DEFAULT '',
            note        TEXT DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
        """
    )
    rows = conn.execute(
        "SELECT id, nickname, note, active, created_at FROM players"
    ).fetchall()
    for r in rows:
        created = r["created_at"] or _now()
        try:
            exp = (datetime.fromisoformat(created) + timedelta(days=config.BIND_VALIDITY_DAYS)).isoformat(
                timespec="seconds"
            )
        except Exception:
            exp = _expires(config.BIND_VALIDITY_DAYS)
        conn.execute(
            "INSERT INTO players_new (id, uid, nickname, note, active, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (r["id"], r["nickname"], r["nickname"], r["note"], r["active"], created, exp),
        )
    conn.execute("DROP TABLE players")
    conn.execute("ALTER TABLE players_new RENAME TO players")


def _migrate_codes_v2(conn):
    """兼容旧库：codes 表新增 reward 相关字段与 updated_at。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(codes)").fetchall()}
    if "reward_name" in cols:
        return
    print("[db] 检测到旧版 codes 表，执行迁移到含奖励/过期/状态 schema")
    conn.execute("ALTER TABLE codes ADD COLUMN reward_name TEXT DEFAULT ''")
    conn.execute("ALTER TABLE codes ADD COLUMN reward_qty  TEXT DEFAULT ''")
    conn.execute("ALTER TABLE codes ADD COLUMN reward_icon TEXT DEFAULT ''")
    conn.execute("ALTER TABLE codes ADD COLUMN updated_at  TEXT DEFAULT ''")
    # 旧数据用 created_at 作为首次更新时间
    conn.execute("UPDATE codes SET updated_at = created_at WHERE COALESCE(updated_at,'') = ''")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            -- 玩家表：以 UID 为业务主键；nickname 用于调用官方接口(可为空，为空则用 uid)
            CREATE TABLE IF NOT EXISTS players (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                uid         TEXT NOT NULL UNIQUE,    -- 玩家输入的 UID
                nickname    TEXT DEFAULT '',           -- 游戏昵称(与 UID 不同时，兑换时优先用它)
                note        TEXT DEFAULT '',          -- 备注(如区服说明)
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL            -- 绑定有效期(默认 7 天)
            );

            -- 礼包码表：包含奖励描述、过期时间、更新时间等展示字段
            CREATE TABLE IF NOT EXISTS codes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',          -- 通用描述/来源描述
                reward_name TEXT DEFAULT '',          -- 奖励名称（如 抽、粉、金币）
                reward_qty  TEXT DEFAULT '',          -- 奖励数量（如 x3）
                reward_icon TEXT DEFAULT '',          -- 图标关键字（前端映射）
                expires_at  TEXT,                     -- ISO 时间，可空=永久有效
                updated_at  TEXT NOT NULL,            -- 最后发现/更新时间
                active      INTEGER NOT NULL DEFAULT 1,
                source      TEXT NOT NULL DEFAULT '',
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

        # 旧库迁移
        tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "players" in tables:
            cols = {
                r["name"] for r in conn.execute("PRAGMA table_info(players)").fetchall()
            }
            if "uid" not in cols:
                _migrate_players_v1(conn)
        if "codes" in tables:
            _migrate_codes_v2(conn)


# ---------------- 玩家 ----------------
def add_player(uid: str, nickname: str = "", note: str = "") -> dict:
    uid = uid.strip()
    if not uid:
        raise ValueError("UID 不能为空")
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO players(uid, nickname, note, active, created_at, expires_at) "
            "VALUES(?,?,?,1,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET active=1, nickname=excluded.nickname, "
            "note=excluded.note, expires_at=excluded.expires_at "
            "RETURNING *",
            (uid, nickname.strip(), note, _now(), _expires(config.BIND_VALIDITY_DAYS)),
        )
        return dict(cur.fetchone())


def get_player_by_uid(uid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM players WHERE uid=? AND active=1 AND expires_at > datetime('now')",
            (uid.strip(),),
        ).fetchone()
        return dict(row) if row else None


def list_players(active_only: bool = True) -> list[dict]:
    """列出玩家。默认只返回在有效期内的激活玩家。"""
    q = "SELECT * FROM players"
    if active_only:
        q += " WHERE active=1 AND expires_at > datetime('now')"
    q += " ORDER BY id"
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(q).fetchall()]


def deactivate_player(uid: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute("UPDATE players SET active=0 WHERE uid=?", (uid.strip(),))


# ---------------- 礼包码 ----------------
def _code_status(row: dict) -> str:
    """根据数据库行计算礼包码全局状态。"""
    if not row.get("active"):
        return "inactive"
    exp = row.get("expires_at")
    if exp:
        try:
            if datetime.fromisoformat(exp) < datetime.now(timezone.utc).replace(tzinfo=None):
                return "expired"
        except Exception:
            pass
    return "active"


def _enrich_code(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["status"] = _code_status(d)
    return d


def add_code(
    code: str,
    description: str = "",
    reward_name: str = "",
    reward_qty: str = "",
    reward_icon: str = "",
    expires_at: str | None = None,
    source: str = "manual",
) -> dict:
    code = code.strip().upper()
    if not code:
        raise ValueError("礼包码不能为空")
    now = _now()
    with _lock, get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO codes(code, description, reward_name, reward_qty, reward_icon,
                              expires_at, updated_at, active, source, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(code) DO UPDATE SET
                active=1,
                source=excluded.source,
                updated_at=excluded.updated_at,
                expires_at=COALESCE(excluded.expires_at, codes.expires_at),
                description=COALESCE(NULLIF(excluded.description,''), codes.description),
                reward_name=COALESCE(NULLIF(excluded.reward_name,''), codes.reward_name),
                reward_qty= COALESCE(NULLIF(excluded.reward_qty,''),  codes.reward_qty),
                reward_icon=COALESCE(NULLIF(excluded.reward_icon,''), codes.reward_icon)
            RETURNING *
            """,
            (
                code,
                description.strip(),
                reward_name.strip(),
                reward_qty.strip(),
                reward_icon.strip(),
                expires_at,
                now,
                1,
                source,
                now,
            ),
        )
        return _enrich_code(cur.fetchone())


def add_code_and_enqueue(
    code: str,
    description: str = "",
    reward_name: str = "",
    reward_qty: str = "",
    reward_icon: str = "",
    expires_at: str | None = None,
    source: str = "manual",
) -> tuple[dict, int]:
    """录入礼包码并立即为所有在有效期内玩家生成 pending 任务。返回 (code行, 新建任务数)。"""
    row = add_code(code, description, reward_name, reward_qty, reward_icon, expires_at, source)
    queued = enqueue_redemptions_for_code(row["id"])
    return row, queued


def update_code(
    code: str,
    reward_name: str | None = None,
    reward_qty: str | None = None,
    reward_icon: str | None = None,
    description: str | None = None,
    expires_at: str | None = None,
    active: bool | None = None,
) -> dict | None:
    """管理员精确更新某个礼包码的元数据（空字符串可清空字段）。"""
    code = code.strip().upper()
    fields: list[tuple[str, any]] = []
    if reward_name is not None:
        fields.append(("reward_name", reward_name.strip()))
    if reward_qty is not None:
        fields.append(("reward_qty", reward_qty.strip()))
    if reward_icon is not None:
        fields.append(("reward_icon", reward_icon.strip()))
    if description is not None:
        fields.append(("description", description.strip()))
    if expires_at is not None:
        fields.append(("expires_at", expires_at if expires_at.strip() else None))
    if active is not None:
        fields.append(("active", 1 if active else 0))
    if not fields:
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
            return _enrich_code(row) if row else None

    fields.append(("updated_at", _now()))
    sql = "UPDATE codes SET " + ", ".join(f"{k}=?" for k, _ in fields) + " WHERE code=?"
    values = [v for _, v in fields] + [code]
    with _lock, get_conn() as conn:
        cur = conn.execute(sql, values)
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        return _enrich_code(row)


def delete_code(code: str) -> bool:
    with _lock, get_conn() as conn:
        cur = conn.execute("DELETE FROM codes WHERE code=?", (code.strip().upper(),))
        return cur.rowcount > 0


def deactivate_code(code: str) -> bool:
    """管理员去激活某个礼包码（如自动抓到失效/无效码）。"""
    with _lock, get_conn() as conn:
        cur = conn.execute("UPDATE codes SET active=0 WHERE code=?", (code.strip(),))
        return cur.rowcount > 0


def list_codes(active_only: bool = False, include_expired: bool = True) -> list[dict]:
    q = "SELECT * FROM codes"
    params = []
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY updated_at DESC, id DESC"
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(q, params).fetchall()]
    for r in rows:
        r["status"] = _code_status(r)
    if active_only and not include_expired:
        rows = [r for r in rows if r["status"] != "expired"]
    return rows


# ---------------- 兑换记录 ----------------
def enqueue_redemptions_for_code(code_id: int) -> int:
    """为某礼包码，给所有在有效期内的激活玩家创建 pending 记录。跳过已过期/停用的码。返回新建条数。"""
    with _lock, get_conn() as conn:
        # 码必须处于激活且未过期状态才入队，过期码不再额外排队
        code_ok = conn.execute(
            "SELECT 1 FROM codes WHERE id=? AND active=1 "
            "AND (expires_at IS NULL OR expires_at > datetime('now'))",
            (code_id,),
        ).fetchone()
        if not code_ok:
            return 0
        players = conn.execute(
            "SELECT id FROM players WHERE active=1 AND expires_at > datetime('now')"
        ).fetchall()
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
        # 只给在有效期内的玩家补发（防止绑定入口被滥用）
        player = conn.execute(
            "SELECT id FROM players WHERE id=? AND active=1 AND expires_at > datetime('now')",
            (player_id,),
        ).fetchone()
        if not player:
            return 0
        codes = conn.execute(
            "SELECT id FROM codes WHERE active=1 "
            "AND (expires_at IS NULL OR expires_at > datetime('now'))"
        ).fetchall()
        n = 0
        for c in codes:
            cur = conn.execute(
                "INSERT OR IGNORE INTO redemptions(player_id, code_id, status, updated_at) "
                "VALUES(?,?,'pending',?)",
                (player_id, c["id"], _now()),
            )
            n += cur.rowcount
        return n


def expire_stale_pending() -> int:
    """把已过期礼包码对应的仍处于 pending 的历史任务标记为 expired，避免无谓排队兑换。返回清理条数。"""
    with _lock, get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE redemptions SET status='expired', message='礼包码已过期，已取消兑换', updated_at=?
            WHERE status='pending'
              AND code_id IN (
                  SELECT id FROM codes
                  WHERE active=1
                    AND expires_at IS NOT NULL
                    AND expires_at <= datetime('now')
              )
            """,
            (_now(),),
        )
        return cur.rowcount


def get_pending_jobs() -> list[dict]:
    """取出所有待处理任务，联表带出 UID、昵称与码。过滤过期玩家与过期码。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id AS redemption_id, r.player_id, r.code_id, r.attempts,
                   p.uid, p.nickname, c.code
            FROM redemptions r
            JOIN players p ON p.id = r.player_id
            JOIN codes   c ON c.id = r.code_id
            WHERE r.status = 'pending'
              AND p.active = 1
              AND p.expires_at > datetime('now')
              AND c.active = 1
              AND (c.expires_at IS NULL OR c.expires_at > datetime('now'))
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


def get_player_redemptions(uid: str, limit: int = 1000) -> list[dict]:
    """查询某个 UID 的兑换记录。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id, p.uid, p.nickname, c.code, r.status, r.message, r.attempts, r.updated_at
            FROM redemptions r
            JOIN players p ON p.id = r.player_id
            JOIN codes   c ON c.id = r.code_id
            WHERE p.uid = ?
            ORDER BY r.updated_at DESC
            LIMIT ?
            """,
            (uid.strip(), limit),
        ).fetchall()
        return [dict(r) for r in rows]


def stats() -> dict:
    with get_conn() as conn:
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM redemptions GROUP BY status"
            ).fetchall()
        }
        players = conn.execute(
            "SELECT COUNT(*) AS n FROM players WHERE active=1 AND expires_at > datetime('now')"
        ).fetchone()["n"]
        codes = conn.execute("SELECT COUNT(*) AS n FROM codes WHERE active=1").fetchone()["n"]
        total_players = conn.execute("SELECT COUNT(*) AS n FROM players").fetchone()["n"]
        return {
            "players": players,
            "codes": codes,
            "total_players": total_players,
            "redemptions_by_status": by_status,
        }


def recent_redemptions(limit: int = 100) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id, p.uid, p.nickname, c.code, r.status, r.message, r.attempts, r.updated_at
            FROM redemptions r
            JOIN players p ON p.id = r.player_id
            JOIN codes   c ON c.id = r.code_id
            ORDER BY r.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
