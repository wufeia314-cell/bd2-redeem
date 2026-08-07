"""核心兑换逻辑：模拟官方网页请求，调用 BD2 官方兑换接口。

接口契约（逆向自官方兑换中心）：
    POST https://loj2urwaua.execute-api.ap-northeast-1.amazonaws.com/prod/coupon
    Headers: Content-Type: application/json
    Body:    {"appId": "bd2-live", "userId": "<游戏昵称>", "code": "<礼包码>"}
    成功:    {"success": true, ...}
    失败:    {"error": "InvalidCode" | "ExpiredCode" | "AlreadyUsed" | ...}
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

import config

# 官方返回的错误码 → (归一化状态, 中文说明)
# 状态取值：
#   success  兑换成功
#   already  该昵称已兑换过此码（视为完成，不再重试）
#   invalid  码无效 / 格式错误
#   expired  码已过期
#   exceeded 码已达使用上限
#   unavailable 码暂不可用
#   bad_user 昵称错误 / 角色不存在
#   error    其它业务失败
ERROR_MAP = {
    "AlreadyUsed": ("already", "该昵称已兑换过此礼包码"),
    "InvalidCode": ("invalid", "礼包码无效"),
    "ValidationFailed": ("invalid", "礼包码校验失败（格式不对）"),
    "BadRequest": ("invalid", "请求被拒绝（码无效或参数错误）"),
    "ExpiredCode": ("expired", "礼包码已过期"),
    "ExceededUses": ("exceeded", "礼包码已达使用上限"),
    "UnavailableCode": ("unavailable", "礼包码当前不可用"),
    "IncorrectUser": ("bad_user", "游戏昵称错误 / 找不到该角色"),
    "ClaimRewardsFailed": ("error", "领取奖励失败"),
}

# 这些状态属于「最终态」，无需再重试
TERMINAL_STATUSES = {"success", "already", "invalid", "expired", "exceeded", "bad_user"}


@dataclass
class RedeemResult:
    status: str          # success / already / invalid / ... / network_error
    message: str         # 人类可读说明
    raw: dict | None = None
    http_status: int | None = None

    @property
    def is_success(self) -> bool:
        return self.status == "success"

    @property
    def is_terminal(self) -> bool:
        """是否为最终态（不需要再排队重试）。"""
        return self.status in TERMINAL_STATUSES


def _parse_response(http_status: int, body: dict) -> RedeemResult:
    # 官方成功标志
    if body.get("success") is True:
        return RedeemResult("success", "兑换成功，奖励已发送至游戏邮箱", body, http_status)

    err = body.get("error") or body.get("errorCode") or body.get("name") or ""
    status, msg = ERROR_MAP.get(err, ("error", f"未知失败：{err or body}"))
    return RedeemResult(status, msg, body, http_status)


def redeem_once(nickname: str, code: str, client: httpx.Client | None = None) -> RedeemResult:
    """同步执行一次兑换。返回归一化结果。

    仅在网络异常 / 5xx 时返回 network_error（调用方可据此重试）；
    业务失败（无效码、过期等）均为最终态。
    """
    payload = {"appId": config.BD2_APP_ID, "userId": nickname.strip(), "code": code.strip()}
    headers = {
        "Content-Type": "application/json",
        "User-Agent": config.USER_AGENT,
        "Origin": config.BD2_ORIGIN,
        "Referer": config.BD2_REFERER,
        "Accept": "application/json, text/plain, */*",
    }

    own_client = client is None
    if own_client:
        client = httpx.Client(timeout=config.REQUEST_TIMEOUT)
    try:
        resp = client.post(config.BD2_API_ENDPOINT, json=payload, headers=headers)
        # 5xx 视为可重试的临时故障
        if resp.status_code >= 500:
            return RedeemResult("network_error", f"服务端错误 {resp.status_code}", None, resp.status_code)
        try:
            body = resp.json()
        except Exception:
            return RedeemResult("network_error", f"返回非 JSON（HTTP {resp.status_code}）", None, resp.status_code)
        return _parse_response(resp.status_code, body)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        return RedeemResult("network_error", f"网络异常：{e.__class__.__name__}", None, None)
    except Exception as e:  # noqa: BLE001
        return RedeemResult("network_error", f"未知异常：{e}", None, None)
    finally:
        if own_client:
            client.close()


def redeem_with_retry(nickname: str, code: str, client: httpx.Client | None = None) -> RedeemResult:
    """带网络重试的兑换。业务失败不重试。"""
    last = RedeemResult("network_error", "未执行")
    for attempt in range(1, config.MAX_RETRIES + 1):
        last = redeem_once(nickname, code, client=client)
        if last.status != "network_error":
            return last
        if attempt < config.MAX_RETRIES:
            time.sleep(min(2 ** attempt, 8))  # 指数退避
    return last


if __name__ == "__main__":
    # 命令行快速测试： python redeemer.py <昵称> <码>
    import sys
    if len(sys.argv) != 3:
        print("用法: python redeemer.py <游戏昵称> <礼包码>")
        raise SystemExit(1)
    r = redeem_with_retry(sys.argv[1], sys.argv[2])
    print(f"[{r.status}] {r.message}")
    print("原始返回:", r.raw)
