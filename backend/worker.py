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
            jobs = db.get_pending_jobs()
            if not jobs:
                _stop.wait(3.0)  # 空闲时轻量轮询
                continue

            for job in jobs:
                if _stop.is_set():
                    break
                t0 = time.monotonic()

                # 官方接口的 userId：优先用玩家填写的 nickname；若为空则用 uid
                user_id = job["nickname"].strip() or job["uid"].strip()
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
