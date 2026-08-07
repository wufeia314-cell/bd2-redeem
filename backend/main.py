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
import worker


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

# ---------------- 玩家绑定限流（防滥用）----------------
# IP -> 最近绑定时间戳列表；每个 IP 每分钟最多 10 次
_BIND_LIMIT = 10
_BIND_WINDOW = 60.0
_bind_hits: dict[str, list[float]] = {}
_bind_lock = threading.Lock()


def _bind_allowed(ip: str) -> bool:
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
                c["code"], c.get("description", ""), source="auto:community"
            )
            if queued > 0:
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
    nickname: str = Field(..., min_length=1, max_length=24, description="游戏内昵称")
    note: str = Field("", max_length=64)


class CodeReq(BaseModel):
    code: str = Field(..., min_length=1, max_length=20)
    description: str = Field("", max_length=128)
    expires_at: str | None = Field(None, description="ISO 时间，可空")
    source: str = Field("manual", max_length=32)


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if x_admin_token != config.ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="管理员令牌无效")


# ---------------- 玩家端 ----------------
@app.post("/api/bind")
def bind(req: BindReq, request: Request):
    """玩家绑定昵称。绑定后自动为其补齐所有历史激活礼包码（一次绑定，历史自动补发）。"""
    ip = request.client.host if request.client else "unknown"
    if not _bind_allowed(ip):
        raise HTTPException(status_code=429, detail="绑定过于频繁，请稍后再试")
    player = db.add_player(req.nickname, req.note)
    queued = db.enqueue_all_codes_for_player(player["id"])
    return {"ok": True, "player": player, "queued_history_codes": queued}


@app.get("/api/codes")
def public_codes():
    """公开：查看当前生效的礼包码列表（含来源）。"""
    return {"codes": db.list_codes(active_only=True)}


@app.get("/api/status/{nickname}")
def my_status(nickname: str):
    """玩家查询自己的兑换情况。"""
    all_rows = db.recent_redemptions(limit=1000)
    mine = [r for r in all_rows if r["nickname"] == nickname.strip()]
    return {"nickname": nickname, "records": mine}


# ---------------- 管理端 ----------------
@app.post("/admin/codes", dependencies=[Depends(require_admin)])
def admin_add_code(req: CodeReq):
    """管理员录入新礼包码 → 立即为所有已绑定玩家生成 pending 任务，后台自动兑换。"""
    code, queued = db.add_code_and_enqueue(req.code, req.description, req.expires_at, req.source)
    return {"ok": True, "code": code, "queued_players": queued}


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
    return db.stats()


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
