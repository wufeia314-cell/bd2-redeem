"""FastAPI 主程序：礼包码展示 + 手动兑换 + 管理员录码 + 社区自动抓取。

运行：
    cd backend
    pip install -r requirements.txt
    export BD2_ADMIN_TOKEN="你的强口令"   # 务必修改默认令牌
    python run.py                        # 默认监听 0.0.0.0:8000，公网/局域网可访问

玩家端：  http://<本机IP或域名>:8000/       —— 查看当前生效礼包码，填入昵称手动兑换
管理端：  http://<本机IP或域名>:8000/admin  —— 录码、抓取、查看统计

设计要点（手动兑换模式 v2）：
- 不再做「玩家绑定 + 后台自动兑换队列」。玩家在前端点「兑换」，即时调官方接口。
- 玩家昵称与「已兑换」记录由前端 localStorage 持久化，服务端不存，彻底规避 Render 临时磁盘失效。
- 服务端只持久化礼包码清单 + 官方实测状态（official_status），启动即自动抓取，重启丢失也无妨。
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


FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


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
    """抓取全部源并把新码入库（仅入库展示，不再触发自动兑换）。返回统计。"""
    result = fetcher.fetch_all(config.COUPON_SOURCES)
    added = 0
    for c in result.get("codes", []):
        try:
            row = db.add_code(
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
            # 新入库：created_at == updated_at 说明是本次插入的新行
            if row.get("created_at") == row.get("updated_at"):
                added += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] 入库失败 {c['code']}: {exc}")
    result["new_codes_added"] = added
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
    _start_fetch_loop()   # 启动社区自动抓取调度
    yield
    _stop_fetch_loop()


app = FastAPI(title="BD2 礼包码手动兑换系统", version="2.0", lifespan=lifespan)


# ---------------- 全局异常处理器：保证任何错误都返回合法 JSON，绝不空 body ----------------
@app.exception_handler(StarletteHTTPException)
async def _http_exc_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"ok": False, "detail": "请求参数错误", "errors": exc.errors()})


@app.exception_handler(Exception)
async def _unhandled_exc_handler(request: Request, exc: Exception):
    print(f"[ERROR] 未处理异常于 {request.url.path}: {exc}")
    return JSONResponse(status_code=500, content={"ok": False, "detail": f"服务器内部错误：{exc}"})


@app.get("/api/health")
def health():
    """连通性自检：部署/代理是否正常可达。"""
    return {"ok": True, "time": time.time()}


# ---------------- 请求模型 ----------------
class RedeemReq(BaseModel):
    # 不为字段加 min_length，让空/纯空格走到下面的显式校验，返回 400 + 中文提示，
    # 而不是 Pydantic 的 422（那样客户端拿不到友好文案）。
    nickname: str = Field("", max_length=24, description="游戏昵称（即官方兑换身份 userId）")
    code: str = Field("", max_length=20, description="要兑换的礼包码")


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
@app.get("/api/codes")
def public_codes():
    """公开：查看当前生效的礼包码列表（始终隐藏过期/失效码）。"""
    codes = db.list_codes(active_only=True, include_expired=False)
    return {"codes": codes}


@app.post("/api/redeem")
def redeem(req: RedeemReq):
    """玩家手动兑换：用游戏昵称 + 礼包码即时调用官方接口，返回最终结果。

    不做「预先探测昵称」——昵称对错由官方直接判定（bad_user），玩家即时看到反馈，
    比「绑定期拦截」更直接；也避免无谓的官方探测请求触发风控。
    """
    nickname = (req.nickname or "").strip()
    code = (req.code or "").strip().upper()
    if not nickname:
        raise HTTPException(status_code=400, detail="请填写游戏昵称")
    if not code:
        raise HTTPException(status_code=400, detail="礼包码不能为空")

    result = redeemer.redeem_with_retry(nickname, code)
    # 码级错误（与玩家无关，纯粹是码本身问题）→ 回写为社区共享的官方状态，
    # 让其他玩家看到真实状态，不再徒劳尝试该码。
    if result.status in ("expired", "invalid", "exceeded", "unavailable"):
        try:
            db.mark_code_official_status(code, result.status)
        except Exception as exc:  # noqa: BLE001
            print(f"[redeem] 回写官方状态出错 {code}: {exc}")
    return {
        "ok": result.is_success or result.status == "already",
        "status": result.status,
        "message": result.message,
    }


class NickReq(BaseModel):
    nickname: str = Field("", max_length=24)


@app.post("/api/verify-nickname")
def verify_nickname(req: NickReq):
    """验证游戏昵称是否存在（用官方探测，不消耗真实礼包码）。"""
    nickname = (req.nickname or "").strip()
    if not nickname:
        return {"ok": True, "exists": False, "message": "请填写游戏昵称"}
    exists, message = redeemer.verify_nickname(nickname)
    return {"ok": True, "exists": exists, "message": message}


# ---------------- 管理端 ----------------
@app.post("/admin/codes", dependencies=[Depends(require_admin)])
def admin_add_code(req: CodeReq):
    """管理员录入新礼包码。"""
    code = db.add_code(
        req.code, req.description, req.reward_name, req.reward_qty,
        req.reward_icon, req.reward_icon_url, req.expires_at, req.source,
    )
    return {"ok": True, "code": code}


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
    """管理员删除礼包码。"""
    ok = db.delete_code(code)
    if not ok:
        raise HTTPException(status_code=404, detail="礼包码不存在")
    return {"ok": True}


@app.post("/admin/codes/{code}/deactivate", dependencies=[Depends(require_admin)])
def admin_deactivate_code(code: str):
    """管理员去激活某个礼包码。"""
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


# ---------------- 前端静态页 ----------------
# 注意：HTML 必须 no-cache。否则手机浏览器会长期缓存旧版 index.html，
# 表现为「桌面已更新的功能，手机端看不到 / 卡在旧逻辑」（曾出现的真实问题）。
_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/")
def index():
    return FileResponse(
        os.path.join(FRONTEND_DIR, "index.html"), headers=_NO_CACHE
    )


@app.get("/admin")
def admin_page():
    return FileResponse(
        os.path.join(FRONTEND_DIR, "admin.html"), headers=_NO_CACHE
    )


# 其它静态资源（如有）
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
