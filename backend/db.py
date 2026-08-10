"""SQLite 数据层：玩家表(以游戏昵称为唯一身份+14天有效期)、礼包码表(含奖励/过期/状态)、兑换记录关联表。"""
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


def _migrate_players_to_nickname(conn):
    """兼容旧库：原 players 表以 uid 为主键，现改回以 nickname 为唯一身份（官方兑换只认昵称）。"""
    print("[db] 检测到旧版 players 表(uid 主键)，迁移到 nickname 身份 schema")
    conn.execute(
        """
        CREATE TABLE players_new (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            nickname    TEXT NOT NULL UNIQUE,    -- 玩家游戏昵称，即官方兑换身份 userId
            note        TEXT DEFAULT '',
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL
        )
        """
    )
    rows = conn.execute(
        "SELECT id, uid, nickname, note, active, created_at FROM players"
    ).fetchall()
    for r in rows:
        # 身份 = 昵称优先；旧库中只填了 UID 的，用 UID 兜底（旧记录兑换时官方会判昵称错，仅保留展示）
        identity = (r["nickname"] or "").strip() or (r["uid"] or "").strip()
        if not identity:
            continue
        created = r["created_at"] or _now()
        try:
            exp = (datetime.fromisoformat(created) + timedelta(days=config.BIND_VALIDITY_DAYS)).isoformat(
                timespec="seconds"
            )
        except Exception:
            exp = _expires(config.BIND_VALIDITY_DAYS)
        conn.execute(
            "INSERT OR IGNORE INTO players_new (id, nickname, note, active, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (r["id"], identity, r["note"], r["active"], created, exp),
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
            -- 玩家表：以「游戏昵称」为唯一身份（官方兑换接口只认昵称，昵称即 userId）
            CREATE TABLE IF NOT EXISTS players (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nickname    TEXT NOT NULL UNIQUE,    -- 玩家游戏昵称，即官方兑换身份 userId
                note        TEXT DEFAULT '',          -- 备注(可选)
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL,
                expires_at  TEXT NOT NULL            -- 绑定有效期(默认 14 天)
            );

            -- 礼包码表：包含奖励描述、过期时间、更新时间等展示字段
            CREATE TABLE IF NOT EXISTS codes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                code        TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',          -- 通用描述/来源描述
                reward_name TEXT DEFAULT '',          -- 奖励名称（如 抽、粉、金币）
                reward_qty  TEXT DEFAULT '',          -- 奖励数量（如 x3）
                reward_icon TEXT DEFAULT '',          -- 图标关键字（前端映射）
                reward_icon_url TEXT DEFAULT '',      -- 图标图片 URL（GameKee 提供，可选）
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

            -- 元数据键值表：用于持久化「参与者计数」等不应随数据重置的标量。
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
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
            if "uid" in cols:
                _migrate_players_to_nickname(conn)
        if "codes" in tables:
            _migrate_codes_v2(conn)
        # codes 表补充「官方真实状态」列，兼容旧库（不验证就显示永久有效是误导）
        try:
            conn.execute(
                "ALTER TABLE codes ADD COLUMN official_status TEXT DEFAULT 'pending'"
            )
        except Exception:
            pass
        # codes 表补充「码上线时间」列：填的是官方/源站公布上线的时间，而非抓取时间；
        # 来源无法提供上线时间时留空，前端据此隐藏该项。
        try:
            conn.execute(
                "ALTER TABLE codes ADD COLUMN published_at TEXT DEFAULT ''"
            )
        except Exception:
            pass
        # codes 表补充「图标图片 URL」列（来自 GameKee 的 image 字段，可选）
        try:
            conn.execute(
                "ALTER TABLE codes ADD COLUMN reward_icon_url TEXT DEFAULT ''"
            )
        except Exception:
            pass
        # 参与者计数：仅在「尚未初始化」时写入基数，绝不覆盖已累加的值（永不重置/清零）。
        _ensure_participants(conn)


# ---------------- 参与者计数（永不重置） ----------------
def _ensure_participants(conn) -> None:
    """仅在 meta 中尚无 participants 键时写入基数；已存在则保留累加值，绝不覆盖/清零。"""
    row = conn.execute("SELECT 1 FROM meta WHERE key='participants'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('participants', ?)",
            (str(config.PARTICIPANT_BASE),),
        )


def _inc_participants(conn) -> None:
    """原子地把参与者计数 +1。只在新玩家绑定（首次插入）时调用，保证单向增长。"""
    conn.execute(
        "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
        "WHERE key='participants'"
    )


def get_participant_count() -> int:
    with get_conn() as conn:
        _ensure_participants(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='participants'").fetchone()
        try:
            return int(row["value"])
        except Exception:
            return config.PARTICIPANT_BASE


# ---------------- 玩家 ----------------
def add_player(nickname: str, note: str = "") -> dict:
    nickname = (nickname or "").strip()
    if not nickname:
        raise ValueError("游戏昵称不能为空")
    with _lock, get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM players WHERE nickname=?", (nickname,)
        ).fetchone()
        is_new = existing is None
        cur = conn.execute(
            "INSERT INTO players(nickname, note, active, created_at, expires_at) "
            "VALUES(?,?,1,?,?) "
            "ON CONFLICT(nickname) DO UPDATE SET active=1, note=excluded.note, "
            "expires_at=excluded.expires_at "
            "RETURNING *",
            (nickname, note, _now(), _expires(config.BIND_VALIDITY_DAYS)),
        )
        row = dict(cur.fetchone())
        # 仅在真正新增玩家时累加参与者计数；已有玩家重新绑定（续期）不重复累加
        if is_new:
            _inc_participants(conn)
        # 派生字段（非表列）：供接口区分「首次绑定」与「续期」，以便给出不同提示
        row["_is_new"] = is_new
        return row


def get_player_by_nickname(nickname: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM players WHERE nickname=? AND active=1 AND expires_at > datetime('now')",
            (nickname.strip(),),
        ).fetchone()
        return dict(row) if row else None


def get_player_any(nickname: str) -> dict | None:
    """取玩家行（不限 active/有效期），用于展示绑定倒计时（含已过期）。"""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM players WHERE nickname=?", (nickname.strip(),)
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


def deactivate_player(nickname: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute("UPDATE players SET active=0 WHERE nickname=?", (nickname.strip(),))


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
    reward_icon_url: str = "",
    expires_at: str | None = None,
    source: str = "manual",
    published_at: str | None = None,
) -> dict:
    code = code.strip().upper()
    if not code:
        raise ValueError("礼包码不能为空")
    now = _now()
    d_desc = description.strip()
    d_rname = reward_name.strip()
    d_qty = reward_qty.strip()
    d_icon = reward_icon.strip()
    d_icon_url = reward_icon_url.strip()
    with _lock, get_conn() as conn:
        existing = conn.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        if existing:
            # 内容无变化时跳过更新，保留原有 updated_at —— 否则每次抓取都把全部码刷成
            # 「最新」，会让列表的「最近更新在前」排序语义失效。
            content_same = (
                (existing["reward_name"] or "") == d_rname
                and (existing["reward_qty"] or "") == d_qty
                and (existing["reward_icon"] or "") == d_icon
                and (existing["reward_icon_url"] or "") == d_icon_url
                and (existing["description"] or "") == d_desc
                and (existing["expires_at"] or None) == (expires_at or None)
                and (existing["published_at"] or "") == (published_at or "")
                and (existing["source"] or "") == source
                and existing["active"] == 1
            )
            if content_same:
                return _enrich_code(existing)
            conn.execute(
                """
                UPDATE codes SET
                    active=1,
                    source=?,
                    updated_at=?,
                    published_at=COALESCE(NULLIF(?,''), published_at),
                    expires_at=COALESCE(?, expires_at),
                    description=COALESCE(NULLIF(?,''), description),
                    reward_name=COALESCE(NULLIF(?,''), reward_name),
                    reward_qty= COALESCE(NULLIF(?,''),  reward_qty),
                    reward_icon=COALESCE(NULLIF(?,''), reward_icon),
                    reward_icon_url=COALESCE(NULLIF(?,''), reward_icon_url)
                WHERE code=?
                """,
                (
                    source, now,
                    published_at or "", expires_at,
                    d_desc, d_rname, d_qty, d_icon, d_icon_url,
                    code,
                ),
            )
            row = conn.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
            return _enrich_code(row)
        conn.execute(
            """
            INSERT INTO codes(code, description, reward_name, reward_qty, reward_icon,
                              reward_icon_url,
                              expires_at, published_at, updated_at, active, source, created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                code, d_desc, d_rname, d_qty, d_icon, d_icon_url,
                expires_at, published_at, now, 1, source, now,
            ),
        )
        # 注意：带 ALTER 追加列的表上用 INSERT ... RETURNING * 在本环境取不到行，
        # 故插入后显式 SELECT（与 UPDATE 分支一致），更稳妥。
        row = conn.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        return _enrich_code(row)


def add_code_and_enqueue(
    code: str,
    description: str = "",
    reward_name: str = "",
    reward_qty: str = "",
    reward_icon: str = "",
    reward_icon_url: str = "",
    expires_at: str | None = None,
    source: str = "manual",
    published_at: str | None = None,
) -> tuple[dict, int]:
    """录入礼包码并立即为所有在有效期内玩家生成 pending 任务。返回 (code行, 新建任务数)。"""
    row = add_code(code, description, reward_name, reward_qty, reward_icon, reward_icon_url, expires_at, source, published_at)
    queued = enqueue_redemptions_for_code(row["id"])
    return row, queued


def update_code(
    code: str,
    reward_name: str | None = None,
    reward_qty: str | None = None,
    reward_icon: str | None = None,
    reward_icon_url: str | None = None,
    description: str | None = None,
    expires_at: str | None = None,
    active: bool | None = None,
    official_status: str | None = None,
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
    if reward_icon_url is not None:
        fields.append(("reward_icon_url", reward_icon_url.strip()))
    if description is not None:
        fields.append(("description", description.strip()))
    if expires_at is not None:
        fields.append(("expires_at", expires_at if expires_at.strip() else None))
    if active is not None:
        fields.append(("active", 1 if active else 0))
    # 管理员可把被误标的码重置回 'pending'（重新参与兑换），或手动置为 valid/expired 等
    if official_status is not None:
        fields.append(("official_status", official_status.strip()))
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


def mark_code_official_status(code_id: int, status: str) -> None:
    """回写礼包码的官方真实状态（pending/valid/expired/invalid/exceeded/unavailable）。

    当 worker 实测某码被官方判定为过期/无效等码级错误时调用，使前端能展示
    真实状态，而不是一直显示社区标注的「永久有效」。
    """
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE codes SET official_status=?, updated_at=? WHERE id=?",
            (status, _now(), code_id),
        )


# worker 实测判定的「已死」官方状态：这些码视为失效/过期，默认不应进入生效列表
_DEAD_OFFICIAL = {"expired", "invalid", "exceeded", "unavailable"}


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
        # 既按日期过期、也按官方实测失效态过滤（official_status 为 dead 的码同样隐藏）
        rows = [
            r
            for r in rows
            if r["status"] != "expired"
            and (r.get("official_status") or "pending") not in _DEAD_OFFICIAL
        ]
    return rows


# ---------------- 兑换记录 ----------------
def enqueue_redemptions_for_code(code_id: int) -> int:
    """为某礼包码，给所有在有效期内的激活玩家创建 pending 记录。跳过已过期/停用/官方已判失效的码。返回新建条数。"""
    with _lock, get_conn() as conn:
        # 码必须处于激活、未过期、且官方状态未被判为失效，才入队
        code_ok = conn.execute(
            "SELECT 1 FROM codes WHERE id=? AND active=1 "
            "AND (expires_at IS NULL OR expires_at > datetime('now')) "
            "AND (official_status IS NULL OR official_status IN ('pending','valid'))",
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
            "AND (expires_at IS NULL OR expires_at > datetime('now')) "
            "AND (official_status IS NULL OR official_status IN ('pending','valid'))"
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
    """把已无法兑换的礼包码对应的残留 pending 任务标记为 canceled，避免无谓排队/堆积。

    无法兑换的情形：
      ① 码被管理员停用(active=0)；② 码已过期；
      ③ 已被官方实测判为失效(invalid/expired/exceeded/unavailable)。

    注意用独立状态 'canceled' 而非 'expired'：'expired' 是官方判定「码已过期」的
    真实兑换结果，两者语义不同；且独立状态才能在码恢复可用时精确找回这些记录
    （见 revive_canceled_for_code），否则 INSERT OR IGNORE 会让它们永远无法回到队列。
    返回清理条数。"""
    with _lock, get_conn() as conn:
        cur = conn.execute(
            """
            UPDATE redemptions SET status='canceled', message='礼包码已失效/停用，已取消兑换', updated_at=?
            WHERE status='pending'
              AND code_id IN (
                  SELECT id FROM codes
                  WHERE active=0
                     OR (expires_at IS NOT NULL AND expires_at <= datetime('now'))
                     OR official_status IN ('expired','invalid','exceeded','unavailable')
              )
            """,
            (_now(),),
        )
        return cur.rowcount


# 「可通过重新绑定自救」的失败态：问题出在玩家侧或网络侧，换个时间/改对昵称就可能成功。
# 不含 success/already（已完成，重试无意义且会白白消耗官方请求），
# 也不含 invalid/expired/exceeded/unavailable（码本身的问题，对谁都一样，重试必然再失败）。
_RETRIABLE_STATUSES = ("failed", "bad_user", "error")


def reset_retriable_redemptions(player_id: int) -> int:
    """把该玩家「可重试的失败记录」重置为 pending，让「重新绑定」成为玩家的自救手段。

    背景：enqueue_all_codes_for_player 用的是 INSERT OR IGNORE，已存在的记录不会被
    改动。因此玩家一旦因网络抖动被判 failed、或因昵称填错被判 bad_user，重新绑定
    也不会重跑，该码对他永远停在失败态且毫无补救办法。

    只重置那些「码本身仍可兑换」的记录，避免把注定失败的任务重新塞回队列。
    返回重置条数。"""
    placeholders = ",".join("?" for _ in _RETRIABLE_STATUSES)
    with _lock, get_conn() as conn:
        cur = conn.execute(
            f"""
            UPDATE redemptions
               SET status='pending', message='重新绑定后自动重试', updated_at=?
             WHERE player_id=?
               AND status IN ({placeholders})
               AND code_id IN (
                   SELECT id FROM codes
                    WHERE active=1
                      AND (expires_at IS NULL OR expires_at > datetime('now'))
                      AND (official_status IS NULL OR official_status IN ('pending','valid'))
               )
            """,
            (_now(), player_id, *_RETRIABLE_STATUSES),
        )
        return cur.rowcount


def revive_canceled_for_code(code_id: int) -> int:
    """礼包码恢复可用时（管理员重新激活/重置官方状态/延长有效期），把此前因该码失效
    而被取消的任务放回队列。否则 INSERT OR IGNORE 会导致这些玩家永远漏兑此码。
    返回恢复条数。"""
    with _lock, get_conn() as conn:
        code_ok = conn.execute(
            "SELECT 1 FROM codes WHERE id=? AND active=1 "
            "AND (expires_at IS NULL OR expires_at > datetime('now')) "
            "AND (official_status IS NULL OR official_status IN ('pending','valid'))",
            (code_id,),
        ).fetchone()
        if not code_ok:
            return 0
        cur = conn.execute(
            "UPDATE redemptions SET status='pending', message='礼包码已恢复，重新排队', updated_at=? "
            "WHERE code_id=? AND status='canceled'",
            (_now(), code_id),
        )
        return cur.rowcount


def get_pending_jobs() -> list[dict]:
    """取出所有待处理任务，联表带出昵称与码。过滤过期玩家与过期码。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id AS redemption_id, r.player_id, r.code_id, r.attempts,
                   p.nickname, c.code
            FROM redemptions r
            JOIN players p ON p.id = r.player_id
            JOIN codes   c ON c.id = r.code_id
            WHERE r.status = 'pending'
              AND p.active = 1
              AND p.expires_at > datetime('now')
              AND c.active = 1
              AND (c.expires_at IS NULL OR c.expires_at > datetime('now'))
              AND (c.official_status IS NULL OR c.official_status IN ('pending', 'valid'))
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


def get_player_redemptions(nickname: str, limit: int = 1000) -> list[dict]:
    """查询某个游戏昵称的兑换记录。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT r.id, p.nickname, c.code, r.status, r.message, r.attempts, r.updated_at
            FROM redemptions r
            JOIN players p ON p.id = r.player_id
            JOIN codes   c ON c.id = r.code_id
            WHERE p.nickname = ?
            ORDER BY r.updated_at DESC
            LIMIT ?
            """,
            (nickname.strip(), limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_player_code_statuses(nickname: str) -> dict[str, str]:
    """返回某昵称对每个礼包码的兑换状态：{code: status}。"""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.code, r.status
            FROM redemptions r
            JOIN players p ON p.id = r.player_id
            JOIN codes   c ON c.id = r.code_id
            WHERE p.nickname = ?
            """,
            (nickname.strip(),),
        ).fetchall()
        return {r["code"]: r["status"] for r in rows}


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
