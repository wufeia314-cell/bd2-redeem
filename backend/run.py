"""启动入口：读取 config.HOST/PORT，启动 FastAPI（默认监听 0.0.0.0，公网/局域网可访问）。

用法：
    cd backend
    pip install -r requirements.txt
    export BD2_ADMIN_TOKEN="你的强口令"
    python run.py
或自定义：
    BD2_HOST=0.0.0.0 BD2_PORT=8000 python run.py
"""
from __future__ import annotations

import config

import uvicorn

if __name__ == "__main__":
    print(f"BD2 兑换系统启动：http://{config.HOST}:{config.PORT}/  (HOST={config.HOST})")
    if config.FETCH_ENABLED:
        print(f"社区自动抓取：开启，每 {config.FETCH_INTERVAL_MIN} 分钟一轮")
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
        log_level="info",
    )
