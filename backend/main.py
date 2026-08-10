"""FastAPI 主程序：玩家绑定 + 管理员录码 + 社区自动抓取 + 自动兑换触发 + 统计。

运行：
    cd backend
    pip install -r requirements.txt
    export BD2_ADMIN_TOKEN="你的强口令"   # 务必修改默认令牌
    python run.py                        # 默认监听 0.0.0.0:8000，公网/局域网可访问

玩家端：  http://<本机IP或域名>:8000/
管理端：  http://<本机IP或域名>:8000/admin
"""
from __future__ import annotations

import os
import threading
import time
import hmac
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

import config
import db
import fetcher
import redeemer
import worker


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

# ---------------- 玩家绑定限流（防滥用）----------------
# IP -> 最近绑定时间戳列表；每个 IP 每分钟最多 10 次
_BIND_LIMIT = 10
_BIND_WINDOW = 60.0
_bind_hits: dict[str, list[float]] = {}
_bind_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    # 部署在反代/负载均衡（如 Render、Nginx）之后时，request.client.host 是内网地址，
    # 所有用户会被误判为同一 IP。优先取 X-Forwarded-For 的第一个真实地址。
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _bind_allowed(request: Request) -> bool:
    ip = _client_ip(request)
    now = time.monotonic()
    with _bind_lock:
        hits = _bind_hits.get(ip, [])
        hits = [t for t in hits if now - t < _BIND_WINDOW]
        if len(hits) >= _BIND_LIMIT:
            _bind_hits[ip] = hits
            return False
        hits.append(now)
        _bind_hits[ip] = hits
        return True


# ---------------- 社区自动抓取调度（后台线程）----------------
_fetch_stop = threading.Event()
_fetch_thread: threading.Thread | None = None


def _fetch_loop() -> None:
    interval = max(1.0, config.FETCH_INTERVAL_MIN * 60.0)
    # 服务启动/唤醒后先立即抓一次，不必干等满一个间隔（对会休眠的免费实例尤其友好）
    if not _fetch_stop.is_set():
        try:
            _run_fetch()
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] 启动抓取异常：{exc}")
    while not _fetch_stop.is_set():
        _fetch_stop.wait(interval)
        if _fetch_stop.is_set():
            break
        try:
            _run_fetch()
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] 自动抓取异常：{exc}")


def _run_fetch() -> dict:
    """抓取全部源并把新码入库（入库即触发自动兑换）。返回统计。"""
    result = fetcher.fetch_all(config.COUPON_SOURCES)
    added = 0
    for c in result.get("codes", []):
        try:
            row, queued = db.add_code_and_enqueue(
                c["code"],
                c.get("description", ""),
                c.get("reward_name", ""),
                c.get("reward_qty", ""),
                c.get("reward_icon", ""),
                c.get("reward_icon_url", ""),
                c.get("expires_at"),
                source=c.get("source") or "auto:community",
                published_at=c.get("published_at"),
            )
            # 新入库：created_at == updated_at 说明是本次插入的新行；
            # queued>0 说明有玩家被排入兑换队列（兼容 players 非空的情况）
            if (row.get("created_at") == row.get("updated_at")) or queued > 0:
                added += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] 入库失败 {c['code']}: {exc}")
    result["new_codes_enqueued"] = added
    print(f"[scheduler] 抓取完成：候选 {result['total_candidates']} 个，新入库 {added} 个")
    return result


def _start_fetch_loop() -> None:
    global _fetch_thread
    if not config.FETCH_ENABLED:
        print("[scheduler] 自动抓取已关闭（BD2_FETCH_ENABLED=0）")
        return
    if _fetch_thread and _fetch_thread.is_alive():
        return
    _fetch_stop.clear()
    _fetch_thread = threading.Thread(target=_fetch_loop, name="bd2-fetch", daemon=True)
    _fetch_thread.start()


def _stop_fetch_loop() -> None:
    _fetch_stop.set()
    if _fetch_thread:
        _fetch_thread.join(timeout=5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if config.ADMIN_TOKEN == "change-me-admin-token":
        print("[SECURITY] 警告：正在使用默认管理员令牌 change-me-admin-token！"
              "请通过环境变量 BD2_ADMIN_TOKEN 设置一个强口令，否则管理后台形同裸奔。")
    worker.start()        # 启动后台限速兑换线程
    _start_fetch_loop()   # 启动社区自动抓取调度
    yield
    _stop_fetch_loop()
    worker.stop()


app = FastAPI(title="BD2 礼包码自动兑换系统", version="1.1", lifespan=lifespan)


# ---------------- 全局异常处理器：保证任何错误都返回合法 JSON，绝不空 body ----------------
@app.exception_handler(StarletteHTTPException)
async def _http_exc_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"ok": False, "detail": "请求参数错误", "errors": exc.errors()})


@app.exception_handler(Exception)
async def _unhandled_exc_handler(request: Request, exc: Exception):
    # 服务端任何未捕获异常都返回 JSON，避免部署/代理层收到空 body 再被浏览器 r.json() 解析报 SyntaxError
    print(f"[ERROR] 未处理异常于 {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"ok": False, "detail": f"服务器内部错误：{exc}"})


@app.get("/api/health")
def health():
    """连通性自检：部署/代理是否正常可达。"""
    return {"ok": True, "time": time.time()}


# ---------------- 请求模型 ----------------
class BindReq(BaseModel):
    nickname: str = Field(..., min_length=1, max_length=24, description="游戏昵称（即官方兑换身份 userId）")
    note: str = Field("", max_length=64)


class CodeReq(BaseModel):
    code: str = Field(..., min_length=1, max_length=20)
    description: str = Field("", max_length=128)
    reward_name: str = Field("", max_length=64, description="奖励名称，如 抽 / 粉 / 金币 / 招募券")
    reward_qty: str = Field("", max_length=16, description="奖励数量，如 x3 / x100000")
    reward_icon: str = Field("", max_length=16, description="图标关键字：gift/ticket/powder/gold/deco/gear/exp")
    reward_icon_url: str = Field("", max_length=512, description="图标图片 URL（优先于关键字图标展示）")
    expires_at: str | None = Field(None, description="ISO 时间，可空")
    source: str = Field("manual", max_length=32)


def require_admin(x_admin_token: str = Header(default="")) -> None:
    # 恒定时间比较，避免令牌逐字符计时攻击
    if not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="管理员令牌无效")


# ---------------- 玩家端 ----------------
@app.post("/api/bind")
def bind(req: BindReq, request: Request):
    """玩家用游戏昵称绑定。绑定后自动为其补齐所有历史激活礼包码，有效期 14 天。"""
    if not _bind_allowed(request):
        raise HTTPException(status_code=429, detail="绑定过于频繁，请稍后再试")

    nickname = (req.nickname or "").strip()
    # 先向官方核实昵称是否真实存在：用假码探测，不消耗任何真实礼包码。
    # 昵称不存在时直接拦下，避免玩家绑了个错名字、之后所有兑换全部失败还不知道为什么。
    exists, why = redeemer.verify_nickname(nickname)
    if exists is False:
        raise HTTPException(
            status_code=404,
            detail=f"游戏内找不到昵称「{nickname}」，请核对后重试（注意区分大小写、空格与特殊符号）",
        )

    player = db.add_player(nickname, req.note)
    is_new = bool(player.pop("_is_new", True))
    queued = db.enqueue_all_codes_for_player(player["id"])
    return {
        "ok": True,
        "player": player,
        "is_new": is_new,                     # True=首次绑定，False=续期
        "nickname_verified": exists is True,  # False 表示核实时网络异常，已降级放行
        "queued_history_codes": queued,
        "participants": db.get_participant_count(),
    }


@app.get("/api/codes")
def public_codes(nickname: str | None = None):
    """公开：查看当前礼包码列表（始终隐藏过期/失效码）和参与者总数（基数+实际绑定）。
    若提供 nickname，则在每个码里附加 my_status（该昵称对此码的兑换状态）。"""
    codes = db.list_codes(active_only=True, include_expired=False)
    if nickname and nickname.strip():
        my_statuses = db.get_player_code_statuses(nickname.strip())
        for c in codes:
            c["my_status"] = my_statuses.get(c["code"])
    return {"codes": codes, "participants": db.get_participant_count()}


@app.get("/api/status")
def my_status(nickname: str):
    """玩家用游戏昵称查询自己的兑换情况。

    昵称作为 query 参数传递（而非 path 参数）：昵称可能包含 '/' 等字符，
    若用 path 参数会被路由当成路径分隔符导致 404（FastAPI 对 %2F 的已知限制）。
    """
    records = db.get_player_redemptions(nickname.strip(), limit=1000)
    player = db.get_player_any(nickname.strip())
    return {"nickname": nickname.strip(), "player": player, "records": records}


# ---------------- 管理端 ----------------
@app.post("/admin/codes", dependencies=[Depends(require_admin)])
def admin_add_code(req: CodeReq):
    """管理员录入新礼包码 → 立即为所有已绑定玩家生成 pending 任务，后台自动兑换。"""
    code, queued = db.add_code_and_enqueue(
        req.code,
        req.description,
        req.reward_name,
        req.reward_qty,
        req.reward_icon,
        req.reward_icon_url,
        req.expires_at,
        req.source,
    )
    return {"ok": True, "code": code, "queued_players": queued}


class CodeUpdateReq(BaseModel):
    reward_name: str | None = Field(None, max_length=64)
    reward_qty: str | None = Field(None, max_length=16)
    reward_icon: str | None = Field(None, max_length=16)
    reward_icon_url: str | None = Field(None, max_length=512, description="图标图片 URL（空字符串可清空）")
    description: str | None = Field(None, max_length=128)
    expires_at: str | None = Field(None, description="ISO 时间，传空字符串表示清空/永久有效")
    active: bool | None = Field(None)
    official_status: str | None = Field(None, description="重置官方状态：pending/valid/expired/invalid 等；置 pending 可让失效码重新参与兑换")


@app.get("/admin/codes", dependencies=[Depends(require_admin)])
def admin_list_codes():
    """管理员查看所有礼包码及元数据（含过期状态）。"""
    return {"codes": db.list_codes(active_only=False)}


@app.put("/admin/codes/{code}", dependencies=[Depends(require_admin)])
def admin_update_code(code: str, req: CodeUpdateReq):
    """管理员精确更新礼包码奖励/过期时间/状态。"""
    payload = req.model_dump(exclude_none=True)
    row = db.update_code(code, **payload)
    if not row:
        raise HTTPException(status_code=404, detail="礼包码不存在")
    return {"ok": True, "code": row}


@app.delete("/admin/codes/{code}", dependencies=[Depends(require_admin)])
def admin_delete_code(code: str):
    """管理员删除礼包码及其关联兑换记录。"""
    ok = db.delete_code(code)
    if not ok:
        raise HTTPException(status_code=404, detail="礼包码不存在")
    return {"ok": True}


@app.post("/admin/codes/{code}/deactivate", dependencies=[Depends(require_admin)])
def admin_deactivate_code(code: str):
    """管理员去激活某个礼包码（如自动抓到失效/无效码）。"""
    ok = db.deactivate_code(code)
    return {"ok": ok}


@app.post("/admin/fetch", dependencies=[Depends(require_admin)])
def admin_fetch():
    """手动立即抓取社区兑换码并入库（也由后台定时自动执行）。"""
    return _run_fetch()


@app.get("/admin/sources", dependencies=[Depends(require_admin)])
def admin_sources():
    """查看当前配置的抓取源与抓取开关。"""
    return {
        "fetch_enabled": config.FETCH_ENABLED,
        "fetch_interval_min": config.FETCH_INTERVAL_MIN,
        "sources": config.COUPON_SOURCES,
    }


@app.get("/admin/stats", dependencies=[Depends(require_admin)])
def admin_stats():
    s = db.stats()
    s["participants"] = db.get_participant_count()
    return s


@app.get("/admin/redemptions", dependencies=[Depends(require_admin)])
def admin_redemptions(limit: int = 100):
    return {"records": db.recent_redemptions(limit=limit)}


@app.get("/admin/players", dependencies=[Depends(require_admin)])
def admin_players():
    return {"players": db.list_players(active_only=False)}


# ---------------- 前端静态页 ----------------
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/admin")
def admin_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "admin.html"))


# 其它静态资源（如有）
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
