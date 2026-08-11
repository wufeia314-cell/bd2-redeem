"""SQLite 数据层（手动兑换模式）：只负责管理「当前生效的礼包码」及其官方实测状态。

设计原则：
- 本服务不再做「自动兑换 + 玩家绑定 + 后台队列」，改为玩家在前端点「兑换」即时调官方接口。
- 玩家昵称与「已兑换」记录**不存服务端**，由前端 localStorage 持久化——
  这样 Render 免费实例重启/重新部署后，即使码库清空（启动即自动重新抓取），
  玩家侧数据也不会丢，彻底规避临时磁盘失效问题。
- 服务端只持久化「礼包码清单 + 官方实测状态」（official_status）。
  官方状态由玩家手动兑换时回写（如某码被判过期/无效），是社区共享的轻量信号，
  即便实例重启丢失，下次抓取/兑换也会重新累积，无持久性要求。
"""
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


def _migrate_codes_v2(conn):
    """兼容旧库：codes 表新增 reward 相关字段与 updated_at。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(codes)").fetchall()}
    if "reward_name" in cols:
        return
    print("[db] 检测到旧版 codes 表，执行迁移到含奖励/过期/状态 schema")
    conn.execute("ALTER TABLE codes ADD COLUMN reward_name TEXT DEFAULT ''")
    conn.execute("ALTER TABLE codes ADD COLUMN reward_qty  TEXT DEFAULT ''")
    conn.execute("ALTER TABLE codes ADD COLUMN reward_icon TEXT DEFAULT ''")
    conn.execute("ALTER TABLE codes ADD COLUMN reward_icon_url TEXT DEFAULT ''")
    conn.execute("ALTER TABLE codes ADD COLUMN updated_at  TEXT DEFAULT ''")
    conn.execute("UPDATE codes SET updated_at = created_at WHERE COALESCE(updated_at,'') = ''")


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            -- 礼包码表：包含奖励描述、过期时间、更新时间、官方实测状态等展示字段
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
            """
        )

        # 旧库迁移：codes 表补充「奖励 / 官方真实状态 / 上线时间 / 图标 URL」列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(codes)").fetchall()}
        if "reward_name" not in cols:
            _migrate_codes_v2(conn)
        for col, ddl in (
            ("official_status", "TEXT DEFAULT 'pending'"),
            ("published_at", "TEXT DEFAULT ''"),
            ("reward_icon_url", "TEXT DEFAULT ''"),
        ):
            if col not in cols:
                try:
                    conn.execute(f"ALTER TABLE codes ADD COLUMN {col} {ddl}")
                except Exception:
                    pass


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
    """录入/更新一个礼包码。手动兑换模式下仅入库展示，不再触发后台排队。"""
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
        row = conn.execute("SELECT * FROM codes WHERE code=?", (code,)).fetchone()
        return _enrich_code(row)


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
    fields: list[tuple[str, object]] = []
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


def mark_code_official_status(code: str, status: str) -> None:
    """回写礼包码的官方真实状态（pending/valid/expired/invalid/exceeded/unavailable）。

    当玩家手动兑换某码被官方判定为过期/无效等码级错误时调用，使前端能展示
    真实状态，而不是一直显示社区标注的「永久有效」。这是社区共享的轻量信号。
    """
    with _lock, get_conn() as conn:
        conn.execute(
            "UPDATE codes SET official_status=?, updated_at=? WHERE code=?",
            (status, _now(), code.strip().upper()),
        )


# worker 实测判定的「已死」官方状态：这些码视为失效/过期，默认不应进入生效列表
_DEAD_OFFICIAL = {"expired", "invalid", "exceeded", "unavailable"}


def list_codes(active_only: bool = False, include_expired: bool = True) -> list[dict]:
    q = "SELECT * FROM codes"
    if active_only:
        q += " WHERE active=1"
    q += " ORDER BY updated_at DESC, id DESC"
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(q).fetchall()]
    for r in rows:
        r["status"] = _code_status(r)
    if active_only and not include_expired:
        rows = [
            r
            for r in rows
            if r["status"] != "expired"
            and (r.get("official_status") or "pending") not in _DEAD_OFFICIAL
        ]
    return rows


def stats() -> dict:
    """轻量统计：仅礼包码维度（手动兑换模式下不再统计玩家/兑换记录）。"""
    with get_conn() as conn:
        codes = conn.execute("SELECT COUNT(*) AS n FROM codes WHERE active=1").fetchone()["n"]
        total = conn.execute("SELECT COUNT(*) AS n FROM codes").fetchone()["n"]
        by_status = {
            r["official_status"]: r["n"]
            for r in conn.execute(
                "SELECT official_status, COUNT(*) AS n FROM codes GROUP BY official_status"
            ).fetchall()
        }
        return {"codes": codes, "total_codes": total, "by_official_status": by_status}
