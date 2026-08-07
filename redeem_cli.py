#!/usr/bin/env python3
"""零依赖命令行兑换工具（仅用 Python 标准库，无需安装任何包）。

用途：不启动后端服务，直接测试官方接口 / 手动批量兑换。

用法：
    # 单个兑换
    python redeem_cli.py --nickname "你的昵称" --code BD2025SUMMER

    # 一个昵称批量兑换多个码
    python redeem_cli.py --nickname "你的昵称" --code CODE1 CODE2 CODE3

    # 从文件读取昵称列表（每行一个），批量给所有人兑换一个码
    python redeem_cli.py --nickfile players.txt --code BD2025SUMMER
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
import urllib.error

API_ENDPOINT = "https://loj2urwaua.execute-api.ap-northeast-1.amazonaws.com/prod/coupon"
APP_ID = "bd2-live"

ERROR_MAP = {
    "AlreadyUsed": "已兑换过该码",
    "InvalidCode": "礼包码无效",
    "ValidationFailed": "校验失败（格式不对）",
    "BadRequest": "请求被拒绝（码无效/参数错误）",
    "ExpiredCode": "礼包码已过期",
    "ExceededUses": "已达使用上限",
    "UnavailableCode": "当前不可用",
    "IncorrectUser": "昵称错误/找不到角色",
    "ClaimRewardsFailed": "领取奖励失败",
}


def redeem(nickname: str, code: str, timeout: float = 15.0) -> dict:
    payload = json.dumps({"appId": APP_ID, "userId": nickname, "code": code}).encode()
    req = urllib.request.Request(
        API_ENDPOINT, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            "Origin": "https://redeem.bd2.pmang.cloud",
            "Referer": "https://redeem.bd2.pmang.cloud/bd2/index.html?lang=zh-cn",
            "Accept": "application/json, text/plain, */*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
        except Exception:
            body = {"error": f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return {"status": "network_error", "message": str(e)}

    if body.get("success") is True:
        return {"status": "success", "message": "兑换成功，奖励已发送至游戏邮箱", "raw": body}
    err = body.get("error") or body.get("errorCode") or body.get("name") or ""
    return {"status": err or "error", "message": ERROR_MAP.get(err, f"失败：{err or body}"), "raw": body}


def main():
    ap = argparse.ArgumentParser(description="BD2 零依赖兑换工具")
    ap.add_argument("--nickname", help="单个游戏昵称")
    ap.add_argument("--nickfile", help="昵称列表文件（每行一个）")
    ap.add_argument("--code", nargs="+", required=True, help="一个或多个礼包码")
    ap.add_argument("--qps", type=float, default=2.5, help="每秒请求数上限（默认2.5）")
    args = ap.parse_args()

    nicknames = []
    if args.nickname:
        nicknames.append(args.nickname.strip())
    if args.nickfile:
        with open(args.nickfile, encoding="utf-8") as f:
            nicknames += [ln.strip() for ln in f if ln.strip()]
    if not nicknames:
        ap.error("请用 --nickname 或 --nickfile 指定昵称")

    interval = 1.0 / max(args.qps, 0.1)
    total, ok = 0, 0
    for nick in nicknames:
        for code in args.code:
            total += 1
            r = redeem(nick, code)
            flag = "✅" if r["status"] == "success" else ("➖" if r["status"] == "AlreadyUsed" else "❌")
            if r["status"] == "success":
                ok += 1
            print(f"{flag} [{nick}] {code} -> {r['message']}")
            time.sleep(interval)
    print(f"\n完成：{ok}/{total} 成功")


if __name__ == "__main__":
    main()
