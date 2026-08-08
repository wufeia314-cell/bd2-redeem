"""限速兑换 worker：后台线程轮询 pending 任务，按 QPS 限速调用官方接口。

设计要点：
- 单消费者 + 固定间隔 sleep(1/QPS)，天然限速，保护官方服务器、避免封 IP。
- 业务失败（无效码/过期/已兑换/昵称错）→ 写入最终态，不再重试。
- 网络错误 → 保留 pending，下轮重试；连续失败超过上限则标记 failed。
- 任务状态持久化在 SQLite，进程重启后自动续跑（无需 Redis/Celery）。
  规模变大时，可把本文件替换为 Celery/RQ + Redis，逻辑一致。
"""
from __future__ import annotations

import threading
import time

import httpx

import config
import db
from redeemer import redeem_with_retry

# 网络错误累计尝试超过此值，判定为 failed（避免死循环）
MAX_JOB_ATTEMPTS = 6

_stop = threading.Event()
_thread: threading.Thread | None = None


def _run_loop() -> None:
    interval = 1.0 / max(config.REDEEM_QPS, 0.1)
    # 复用连接，减少握手开销
    with httpx.Client(timeout=config.REQUEST_TIMEOUT) as client:
        while not _stop.is_set():
            # 每轮先把已过期码对应的残留 pending 标记掉，避免无谓排队兑换
            try:
                cleared = db.expire_stale_pending()
                if cleared:
                    print(f"[worker] 已清理 {cleared} 条过期码残留任务")
            except Exception as exc:  # noqa: BLE001
                print(f"[worker] expire_stale_pending 出错: {exc}")

            jobs = db.get_pending_jobs()
            if not jobs:
                _stop.wait(3.0)  # 空闲时轻量轮询
                continue

            for job in jobs:
                if _stop.is_set():
                    break
                t0 = time.monotonic()

                # 官方接口只认昵称（userId 即游戏昵称）；玩家身份本身就是昵称，无需回退。
                user_id = job["nickname"].strip()
                result = redeem_with_retry(user_id, job["code"], client=client)

                if result.status == "network_error":
                    attempts = job["attempts"] + 1
                    if attempts >= MAX_JOB_ATTEMPTS:
                        db.update_redemption(
                            job["redemption_id"], "failed",
                            f"多次网络失败已放弃：{result.message}",
                        )
                    else:
                        # 保留 pending，下轮再试
                        db.update_redemption(
                            job["redemption_id"], "pending",
                            f"网络失败重试中：{result.message}",
                        )
                else:
                    db.update_redemption(job["redemption_id"], result.status, result.message)
                    # 码级错误（与玩家无关，纯粹是码本身问题）→ 回写 codes 真实状态，
                    # 前端据此显示真实状态，并不再徒劳地反复兑换该码
                    if result.status in ("expired", "invalid", "exceeded", "unavailable"):
                        try:
                            db.mark_code_official_status(job["code_id"], result.status)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[worker] 标记码官方状态出错: {exc}")

                # 限速：保证两次请求间隔 >= interval
                elapsed = time.monotonic() - t0
                if elapsed < interval:
                    _stop.wait(interval - elapsed)


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run_loop, name="bd2-redeem-worker", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout=5)
