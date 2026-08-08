"""全局配置。可通过环境变量覆盖。"""
import os

# ---- 官方兑换接口（已逆向自 https://redeem.bd2.pmang.cloud/bd2/ ）----
BD2_API_ENDPOINT = os.getenv(
    "BD2_API_ENDPOINT",
    "https://loj2urwaua.execute-api.ap-northeast-1.amazonaws.com/prod/coupon",
)
BD2_APP_ID = os.getenv("BD2_APP_ID", "bd2-live")

# 伪装成浏览器发起，附带官方页面 Origin/Referer，尽量贴近真实请求
BD2_ORIGIN = "https://redeem.bd2.pmang.cloud"
BD2_REFERER = "https://redeem.bd2.pmang.cloud/bd2/index.html?lang=zh-cn"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# ---- 限速与重试 ----
# 每秒最多兑换请求数（保护官方服务器、避免 IP 被封）。默认 2.5 QPS。
REDEEM_QPS = float(os.getenv("BD2_REDEEM_QPS", "2.5"))
# 单条兑换的网络重试次数（仅针对网络错误 / 5xx，业务失败不重试）
MAX_RETRIES = int(os.getenv("BD2_MAX_RETRIES", "3"))
REQUEST_TIMEOUT = float(os.getenv("BD2_REQUEST_TIMEOUT", "15"))

# ---- 存储 ----
DB_PATH = os.getenv(
    "BD2_DB_PATH",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "bd2.db"),
)

# ---- 管理后台密钥（录码、看统计需要）----
ADMIN_TOKEN = os.getenv("BD2_ADMIN_TOKEN", "change-me-admin-token")

# ---- 网络绑定（非本机玩家可通过链接访问）----
# 默认 0.0.0.0：监听所有网卡，同局域网/部署后公网都可访问。
# 仅本机调试时可设为 127.0.0.1。
HOST = os.getenv("BD2_HOST", "0.0.0.0")
# 优先读云平台注入的 PORT（Render/Heroku 标准），回退 BD2_PORT，再回退 8000
PORT = int(os.getenv("PORT", os.getenv("BD2_PORT", "8000")))

# ---- 玩家绑定有效期 ----
# 玩家绑定后多少天内有效（默认 7 天）。过期后不再为其自动兑换新码，重新绑定可续期。
BIND_VALIDITY_DAYS = int(os.getenv("BD2_BIND_VALIDITY_DAYS", "7"))

# ---- 社区兑换码自动抓取 ----
# 是否启用后台定时抓取（默认开）。设 0 关闭。
FETCH_ENABLED = os.getenv("BD2_FETCH_ENABLED", "1") != "0"
# 抓取间隔（分钟）。默认每天一轮（1440 分钟）。可用 BD2_FETCH_INTERVAL_MIN 覆盖。
FETCH_INTERVAL_MIN = float(os.getenv("BD2_FETCH_INTERVAL_MIN", "1440"))
# 抓取源（社区/攻略站的礼包码汇总页）。可用环境变量 BD2_COUPON_SOURCES 覆盖，
# 格式为 JSON 数组，例如：["https://x.com/a","https://y.com/b"]
_DEFAULT_SOURCES = [
    "https://ucngame.com/codes/brown-dust-2-codes/",
    "https://mobi.gg/en/tips/brown-dust-2-gift-codes",
    # GameKee 棕色尘埃2 Wiki 兑换码页（重 SPA，静态抓取可能为空，留作扩展）：
    # "https://www.gamekee.com/twhj/601290.html",
]
_env_sources = os.getenv("BD2_COUPON_SOURCES")
if _env_sources:
    try:
        import json

        _parsed = json.loads(_env_sources)
        if isinstance(_parsed, list) and all(isinstance(s, str) for s in _parsed):
            COUPON_SOURCES = _parsed
        else:
            COUPON_SOURCES = _DEFAULT_SOURCES
    except Exception:
        COUPON_SOURCES = _DEFAULT_SOURCES
else:
    COUPON_SOURCES = _DEFAULT_SOURCES
