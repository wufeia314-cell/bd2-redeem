"""社区兑换码自动抓取器。

目标：定期从配置的社区/攻略源抓取最新《棕色尘埃2》礼包码，归一化并尽量提取
奖励描述、过期时间等元数据后交给 db 入库，入库即触发自动兑换。

设计要点：
- 不依赖任何单一站点的私有 API，采用「通用提取 + 已知前缀 + 关键词上下文」策略，
  对多数礼包码汇总页都有效，新增源只需往 config.COUPON_SOURCES 里加 URL。
- BD2 礼包码形态相对固定（以 BD2 / 2026BD2 / BURAJO / WAITING4 / THANK / 1YEAR ...
  等开头），据此高置信识别；minified 页面常带 React 伪影（末尾多一个字母），做归一化。
- 提取到的候选码会尝试解析附近的奖励描述与过期时间；英文站多为通用描述，
  可在管理后台手动覆盖成中文奖励信息。
- 若经 worker 实际兑换判定为无效/过期，会被标记且无害，管理员可去激活。
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timedelta
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
CTX_KEYWORDS = re.compile(r"兑换码|礼包码|coupon|code|redeem|gift\s*code", re.I)

# 候选 token：6-20 位字母数字（允许下划线），首字符字母或数字
_CANDIDATE = re.compile(r"\b[A-Z0-9][A-Z0-9_]{5,19}\b")

# 纯 16 进制色值（如 0057FF）误报过滤
_HEX6 = re.compile(r"^[0-9A-Fa-f]{6}$")

# 常见过期时间表达
_EXPIRE_PATTERNS = [
    # Valid until January 31st, 2026
    re.compile(r"Valid\s+until\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", re.I),
    # Expires on January 31, 2026
    re.compile(r"Expires\s+on\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})", re.I),
    # Expiration date: 2026-01-31
    re.compile(r"(?:Expiration|Expiry)\s+date[:：]?\s*(\d{4}-\d{2}-\d{2})", re.I),
    # 有效期至：2026-01-31 / 2026年1月31日
    re.compile(r"有效期[至至][:：]?\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)", re.I),
    # 过期时间：2026-01-31
    re.compile(r"过期时间[:：]?\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}[日]?)", re.I),
]
_RELATIVE_DAYS = re.compile(r"(\d{1,3})\s*天后过期", re.I)
_PERMANENT = re.compile(r"永久有效|永不过期|永久", re.I)

# 奖励数量单位（用于切分 reward_name / reward_qty）
_REWARD_UNIT = re.compile(
    r"(\d[\d\s,\.]*\s*(?:抽|Draw|Ticket|券|钻|Dia|Recruit|招募|粉|Powder|"
    r"金币|Gold|装饰|Deko|Deco|装备|Equipment|经验|EXP|体力|AP|UR|五星|5星|宝石|Jewel|水晶|Crystal))",
    re.I,
)

# 图标关键词映射（供前端显示）
_REWARD_ICON_KEYWORDS = [
    ("ticket", re.compile(r"抽|draw|ticket|券|recruit|招募", re.I)),
    ("powder", re.compile(r"粉|powder|refining", re.I)),
    ("gold",   re.compile(r"金币|gold|dia|钻石|jewel|宝石", re.I)),
    ("deco",   re.compile(r"装饰|deko|deco", re.I)),
    ("gear",   re.compile(r"装备|equipment", re.I)),
    ("exp",    re.compile(r"经验|exp|体力|ap", re.I)),
    ("ticket", re.compile(r"5星|五星|ur\b", re.I)),  # 五星招募券也算 ticket
]


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


# ---------------- 元数据解析 ----------------
def _normalize_date_str(s: str) -> str:
    """去掉日期里的序数词后缀（st/nd/rd/th）方便解析。"""
    return re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s)


def _parse_expiry(window: str) -> str | None:
    """从文本片段里尝试提取过期时间，返回 YYYY-MM-DD 或 None（None=永久有效/未知）。"""
    # 永久有效
    if _PERMANENT.search(window):
        return None
    # 相对“X天后过期”
    m = _RELATIVE_DAYS.search(window)
    if m:
        days = int(m.group(1))
        return (datetime.now().date() + timedelta(days=days)).isoformat()
    # 绝对日期
    for pat in _EXPIRE_PATTERNS:
        m = pat.search(window)
        if not m:
            continue
        raw = _normalize_date_str(m.group(1).strip())
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                pass
        try:
            return datetime.strptime(re.sub(r"[年月]", "-", raw).replace("日", ""), "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    return None


def _extract_reward(window: str) -> dict:
    """从文本片段提取奖励描述、数量、图标。返回 dict。"""
    # 先查找带数量单位的奖励描述
    m = _REWARD_UNIT.search(window)
    if m:
        full = m.group(1).strip()
        # 尝试切分数量和单位
        qm = re.match(r"(\d[\d\s,\.]*)(.*)", full)
        if qm:
            qty = "x" + qm.group(1).replace(" ", "").replace(",", "")
            unit = qm.group(2).strip()
        else:
            qty = ""
            unit = full
        reward_name = unit
        reward_qty = qty
    else:
        # fallback：Redeem this coupon code for exclusive rewards -> 通用描述
        m2 = re.search(
            r"Redeem\s+this\s+coupon\s+code\s+for\s+(.{3,60}?)(?:\s+\(|Valid|Expires|$)",
            window, re.I,
        )
        reward_name = m2.group(1).strip() if m2 else ""
        reward_qty = ""

    # 图标映射
    icon = "gift"
    text_for_icon = reward_name
    if not text_for_icon:
        text_for_icon = window
    for key, pat in _REWARD_ICON_KEYWORDS:
        if pat.search(text_for_icon):
            icon = key
            break

    # 清理描述，避免 HTML 实体或过长
    reward_name = re.sub(r"[\n\r\t]+", " ", reward_name).strip(" ·。,，")
    if len(reward_name) > 80:
        reward_name = reward_name[:80]
    reward_qty = re.sub(r"[\s,]", "", reward_qty) if reward_qty else ""

    return {
        "reward_name": reward_name,
        "reward_qty": reward_qty,
        "reward_icon": icon,
    }


def _best_effort_description(text: str, start: int, end: int) -> str:
    """在码出现位置后方一小段内，尝试抓取奖励描述片段。"""
    window = text[end : end + 130]
    window = re.sub(r"<[^>]+>", " ", window)
    m = re.search(
        r"(\d[\d,]*\s*(?:抽|Draw|Ticket|券|钻|Dia|Recruit|招募|粉|Powder|"
        r"金币|Gold|装饰|Deko|Deco|装备|Equipment|经验|EXP|体力|AP|UR|五星|5星|宝石|Jewel|水晶|Crystal))",
        window,
        re.I,
    )
    if m:
        snippet = window[max(0, m.start() - 10) : m.end() + 8]
        snippet = re.sub(r"\s+", " ", snippet).strip()
        return snippet[:60]
    return ""


def extract_codes(html_text: str, source_url: str = "") -> list[dict]:
    """从一段 HTML 文本中提取礼包码候选。返回包含元数据的 dict 列表。"""
    text = html.unescape(html_text)
    found: dict[str, dict] = {}  # code -> metadata

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

    for idx, (i, j, norm) in enumerate(positions):
        # 已知前缀直接收
        if _is_known_prefix(norm):
            accept = True
        # 否则需在关键词上下文附近
        elif near_keyword(i, j):
            accept = True
        else:
            accept = False

        if not accept:
            continue

        # 只取码后面、到下一个候选码之前的短窗口，避免信息串到下一个码
        next_start = positions[idx + 1][0] if idx + 1 < len(positions) else None
        max_after = j + 180
        if next_start is not None and next_start > j + 60:
            after_end = min(next_start, max_after)
        else:
            after_end = max_after
        after = text[j:after_end]
        after_clean = re.sub(r"<[^>]+>", " ", after)
        # 过期时间常常在码前面（如 08-01更新 23天后过期 CODE）
        before = text[max(0, i - 100) : i]
        before_clean = re.sub(r"<[^>]+>", " ", before)
        reward = _extract_reward(after_clean)
        expires_at = _parse_expiry(after_clean) or _parse_expiry(before_clean)
        description = _best_effort_description(text, i, j)
        if not description and reward["reward_name"]:
            description = f"{reward['reward_qty']} {reward['reward_name']}".strip()

        # 合并重复：保留更具体的奖励描述 / 更早的过期时间
        existing = found.get(norm)
        if existing:
            if not existing.get("reward_name") and reward["reward_name"]:
                existing["reward_name"] = reward["reward_name"]
            if not existing.get("reward_qty") and reward["reward_qty"]:
                existing["reward_qty"] = reward["reward_qty"]
            if not existing.get("expires_at") and expires_at:
                existing["expires_at"] = expires_at
            if not existing.get("description") and description:
                existing["description"] = description
        else:
            found[norm] = {
                "code": norm,
                "description": description,
                "reward_name": reward["reward_name"],
                "reward_qty": reward["reward_qty"],
                "reward_icon": reward["reward_icon"],
                "expires_at": expires_at,
                "updated_at": datetime.now().date().isoformat(),
            }

    # 伪影去重：若 X 与 X+单字符 同时存在，丢弃较长的伪影（保留干净码）
    for c in list(found.keys()):
        if len(c) >= 7 and c[:-1] in found:
            found.pop(c, None)

    return list(found.values())


def _host_of(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url


# ---------------- GameKee 棕色尘埃2 Wiki 兑换码接口（优先源）----------------
# 逆向自其 SPA：码表走后端接口而非 HTML，需带 game-alias 请求头。
# state=2 为当前有效码；state=1 为失效/过期码（不抓取，避免污染有效列表）。
GAMEKEE_API = "https://www.gamekee.com/v1/game/cdk/queryByServerIdPageList"


def _map_gamekee_icon(content: str, ctype: int) -> str:
    """根据 GameKee 的奖励描述与类型枚举映射到前端图标关键字。"""
    c = content or ""
    if re.search(r"抽|招募|ticket|draw|recruit", c, re.I):
        return "ticket"
    if re.search(r"粉|精炼|powder|refining", c, re.I):
        return "powder"
    if re.search(r"钻|dia|diamond|宝石|jewel", c, re.I):
        return "gold"
    if re.search(r"装饰|deco|deko", c, re.I):
        return "deco"
    if re.search(r"装备|equipment|手", c, re.I):
        return "gear"
    # 类型枚举兜底：1=钻 2=抽 3=招募券 4=装饰/装备 5=粉
    return {1: "gold", 2: "ticket", 3: "ticket", 4: "deco", 5: "powder"}.get(ctype, "gift")


def fetch_gamekee_codes(
    alias: str | None = None, server_id: int | None = None
) -> list[dict]:
    """从 GameKee Wiki 接口抓取当前有效兑换码（含中文奖励与精确过期）。

    返回与 extract_codes 相同结构的 dict 列表（code/description/reward_name/
    reward_qty/reward_icon/expires_at/updated_at）。失败返回空列表。
    """
    alias = alias or config.GAMEKEE_GAME_ALIAS
    server_id = server_id or config.GAMEKEE_CDK_SERVER_ID
    headers = {
        "User-Agent": config.USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
        "game-alias": alias,
        "device-num": "1",
        "Lang": "zh-cn",
    }
    out: list[dict] = []
    page = 1
    while True:
        try:
            with httpx.Client(timeout=config.REQUEST_TIMEOUT, headers=headers) as client:
                resp = client.get(
                    GAMEKEE_API,
                    params={
                        "server_id": server_id,
                        "state": 2,  # 当前有效
                        "page_no": page,
                        "page_size": 50,
                    },
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[fetcher] GameKee 抓取失败: {exc}")
            break
        if payload.get("code") != 0 or not payload.get("data"):
            # code!=0（如缺少游戏信息）或空页，停止翻页
            break
        items = payload["data"]
        for it in items:
            code = (it.get("code") or "").strip().upper()
            if not code:
                continue
            end_at = it.get("end_at") or 0
            expires_at = (
                datetime.utcfromtimestamp(end_at).date().isoformat()
                if end_at and end_at > 0
                else None
            )
            content = (it.get("content") or "").strip()
            created = it.get("created_at") or 0
            updated = (
                datetime.utcfromtimestamp(created).date().isoformat()
                if created
                else datetime.now().date().isoformat()
            )
            out.append(
                {
                    "code": code,
                    "description": content,
                    "reward_name": content,
                    "reward_qty": "",
                    "reward_icon": _map_gamekee_icon(content, it.get("type", 0) or 0),
                    "expires_at": expires_at,
                    "updated_at": updated,
                }
            )
        if len(items) < 50:
            break
        page += 1
    return out


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
    """抓取所有配置源，合并去重。返回统计。

    GameKee 接口作为优先源先合并（中文奖励/过期更准确），其余 HTML 源补充去重。
    """
    sources = sources or config.COUPON_SOURCES
    merged: dict[str, dict] = {}
    per_source: dict[str, int] = {}

    # 1) GameKee 优先（若启用）
    if config.GAMEKEE_FETCH_ENABLED:
        gk = fetch_gamekee_codes()
        per_source["gamekee.com(zsca2)"] = len(gk)
        for c in gk:
            merged[c["code"]] = c  # 先入为主，后续源不覆盖

    # 2) 其它 HTML 社区源补充
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
