"""社区兑换码自动抓取器。

目标：定期从配置的社区/攻略源抓取最新《棕色尘埃2》礼包码，归一化后交给 db 入库，
入库即触发自动兑换（worker 会对所有已绑定玩家尝试兑换）。

设计要点：
- 不依赖任何单一站点的私有 API，采用「通用提取 + 已知前缀 + 关键词上下文」策略，
  因此对多数礼包码汇总页都有效，新增源只需往 config.COUPON_SOURCES 里加 URL。
- BD2 礼包码形态相对固定（以 BD2 / 2026BD2 / BURAJO / WAITING4 / THANK / 1YEAR ...
  等开头），据此高置信识别；minified 页面常带 React 伪影（末尾多一个字母），做归一化。
- 提取到的候选码若经 worker 实际兑换判定为无效/过期，会被标记且无害，管理员可去激活。
"""
from __future__ import annotations

import html
import re
import time
from urllib.parse import urlparse

import httpx

import config

# ---------------- 已知礼包码前缀（高置信识别 + 伪影归一化依据）----------------
KNOWN_PREFIXES = (
    "BD2", "2025BD2", "2026BD2", "BD2025", "BD2026",
    "BURAJO", "WAITING4", "THANK", "1YEAR", "50THANK",
    "EMBARKBD", "BD2OPEN", "BD2HALF", "BD2COLLAB", "2026HAPPY",
    "BD2RADIO", "BD2SDCC", "BD2BW", "BD2VSQUARE", "HALLOWEEN",
    "FULLMOON", "2NDANNIVERSARY", "BD2ONEYEAR", "BD2LIVEJP", "BD2ANIMENYC",
    "ROU", "0622", "0403", "2025CHRISTMAS", "BD2APL", "BD2FF",
    "THANKSC", "THANKSKY", "SQUARE", "BD21000", "BD2RADIONY", "BD2RADIOMAG",
    "BD2RADIOABYSS", "BD2RADIOCRUISE", "BD2RADIOFINALE", "BD2RADIO0901",
    "BURAJOMANIA", "BURAJOCODE", "WAITING4",
)

# 已知永久/长期有效码（用于伪影精确纠正）
KNOWN_EXACT = {
    "WAITING4LEGEND", "BD2025SUMMER", "BD2OPEN", "BD2HALF", "BD2ONEYEAR",
    "1STANNIVERSARY", "BD2LIVEJP", "BD2COLLAB", "ROU", "0622", "0403",
    "2NDANNIVERSARYBD2", "THANKYOU1YEAR", "1YEARSTORY5", "1YEARBROADCAST",
    "1YEARLIVECAST", "2025CHRISTMASSANTA", "BD2OPEN100", "BD2THANKS",
    "BD2100DAYS", "THANKS100", "EMBARKBD23RD", "BD2HALFOFT",
}

# 关键词上下文（码出现在这些词附近时置信度提升，可放宽前缀限制）
CTX_KEYWORDS = re.compile(r"兑换码|礼包码|coupon|code|redeem", re.I)

# 候选 token：6-20 位字母数字（允许下划线），首字符字母或数字
_CANDIDATE = re.compile(r"\b[A-Z0-9][A-Z0-9_]{5,19}\b")

# 纯 16 进制色值（如 0057FF）误报过滤
_HEX6 = re.compile(r"^[0-9A-Fa-f]{6}$")


def _is_known_prefix(tok: str) -> bool:
    return any(tok.startswith(p) for p in KNOWN_PREFIXES)


def _looks_like_code(tok: str) -> bool:
    if not (6 <= len(tok) <= 20):
        return False
    if _is_known_prefix(tok):
        return True
    # 混合字母+数字，且不是纯 hex 色值
    has_letter = bool(re.search(r"[A-Za-z]", tok))
    has_digit = bool(re.search(r"[0-9]", tok))
    if has_letter and has_digit and not _HEX6.match(tok):
        return True
    return False


def _lev(a: str, b: str) -> int:
    """简单编辑距离，仅用于单字符纠错，长度差 >2 直接判非候选。"""
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j + 1] + 1, cur[-1] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[-1]


def _correct(tok: str) -> str:
    """纠正 minified/React 造成的单字符伪影：
    - 末尾多一个字母且去掉后恰好是已知完整码；
    - 整体是某已知完整码 + 后缀（如 04037H -> 0403）；
    - 与某已知完整码仅差 1 个字符（如 WAITING4LEGEN -> WAITING4LEGEND）。
    否则原样返回，避免误伤正常码。
    """
    if tok in KNOWN_EXACT:
        return tok
    if len(tok) > 1 and tok[:-1] in KNOWN_EXACT:
        return tok[:-1]
    for k in KNOWN_EXACT:
        if tok.startswith(k) and len(tok) > len(k):
            return k
    if len(tok) >= 6:
        for k in KNOWN_EXACT:
            if _lev(tok, k) == 1:
                return k
    return tok


def _best_effort_description(text: str, start: int, end: int) -> str:
    """在码出现位置后方一小段内，尝试抓取奖励描述片段。"""
    window = text[end : end + 220]
    # 去掉标签残余
    window = re.sub(r"<[^>]+>", " ", window)
    m = re.search(
        r"(\d[\d,]*\s*(?:抽|Draw|Ticket|券|钻|Dia|Recruit|UR|招募|装备|Equipment|粉末|Refining|装饰币))",
        window,
        re.I,
    )
    if m:
        snippet = window[max(0, m.start() - 30) : m.end() + 10]
        snippet = re.sub(r"\s+", " ", snippet).strip()
        return snippet[:80]
    return ""


def extract_codes(html_text: str, source_url: str = "") -> list[dict]:
    """从一段 HTML 文本中提取礼包码候选。返回 [{code, description}]。"""
    text = html.unescape(html_text)
    found: dict[str, str] = {}  # code -> description

    # 通用候选扫描（首轮：已知前缀；同时记录位置用于上下文判断）
    positions: list[tuple[int, int, str]] = []
    for m in _CANDIDATE.finditer(text):
        tok = m.group(0)
        if not _looks_like_code(tok):
            continue
        norm = _correct(tok)
        positions.append((m.start(), m.end(), norm))

    # 关键词上下文位置
    ctx_spans = [(m.start(), m.end()) for m in CTX_KEYWORDS.finditer(text)]

    def near_keyword(i: int, j: int) -> bool:
        for (cs, ce) in ctx_spans:
            if abs(cs - j) <= 90 or abs(ce - i) <= 90:
                return True
        return False

    for (i, j, norm) in positions:
        # 已知前缀直接收
        if _is_known_prefix(norm):
            found.setdefault(norm, _best_effort_description(text, i, j))
        # 否则需在关键词上下文附近
        elif near_keyword(i, j):
            found.setdefault(norm, _best_effort_description(text, i, j))

    # 伪影去重：若 X 与 X+单字符 同时存在，丢弃较长的伪影（保留干净码）
    for c in list(found.keys()):
        if len(c) >= 7 and c[:-1] in found:
            found.pop(c, None)

    return [{"code": c, "description": d} for c, d in found.items()]


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


def fetch_source(url: str, timeout: float | None = None) -> list[dict]:
    """抓取单个源并提取候选码。失败返回空列表（不中断其它源）。"""
    timeout = timeout or config.REQUEST_TIMEOUT
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": config.USER_AGENT}
        ) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return extract_codes(resp.text, url)
    except Exception as exc:  # noqa: BLE001 - 单源失败不应影响整体
        print(f"[fetcher] 源抓取失败 {url}: {exc}")
        return []


def fetch_all(sources: list[str] | None = None) -> dict:
    """抓取所有配置源，合并去重。返回统计。"""
    sources = sources or config.COUPON_SOURCES
    merged: dict[str, dict] = {}
    per_source: dict[str, int] = {}
    for url in sources:
        codes = fetch_source(url)
        per_source[_host_of(url)] = len(codes)
        for c in codes:
            merged.setdefault(c["code"], c)
    return {
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": per_source,
        "total_candidates": len(merged),
        "codes": list(merged.values()),
    }


if __name__ == "__main__":
    import json
    import sys

    # 本地调试：python fetcher.py /path/to.html  （用已保存的页面测试提取）
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8", errors="ignore") as f:
            txt = f.read()
        res = extract_codes(txt, sys.argv[1])
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(fetch_all(), ensure_ascii=False, indent=2))
