# 棕色尘埃2（BrownDust2）礼包码自动兑换系统

仿 GameKee Wiki 的礼包码托管：**玩家用游戏昵称绑定一次，管理员之后每录入一个新码，系统就自动帮所有有效期内玩家兑换**，奖励直接发到游戏邮箱。

底层直接调用官方公开兑换接口（纯 HTTP，不开浏览器、不模拟点击），高效稳定，适合部署在服务器定时/常驻运行。

---

## 一、官方接口（已逆向 + 实测可用）

来源：官方兑换中心 `https://redeem.bd2.pmang.cloud/bd2/index.html`（Preact 单页应用，逻辑在 `config/settings.js` + `services/api-client.js`）。

| 项 | 值 |
|---|---|
| 接口 URL | `POST https://loj2urwaua.execute-api.ap-northeast-1.amazonaws.com/prod/coupon` |
| 请求头 | `Content-Type: application/json`（建议带浏览器 UA + Origin/Referer） |
| 请求体 | `{"appId":"bd2-live","userId":"<游戏昵称>","code":"<礼包码>"}` |
| 成功返回 | `{"success": true, ...}` |
| 失败返回 | `{"error":"<错误码>", ...}` |

**要点：BD2 全球兑换接口只需「userId + 码」，没有区服参数**（`userId` 就是游戏内昵称，系统以玩家填写的游戏昵称为唯一身份调用官方接口，奖励发到该角色邮箱）。

### 错误码对照

| 官方错误码 | 含义 | 系统处理 |
|---|---|---|
| `success:true` | 成功 | 标记 success，不再重试 |
| `AlreadyUsed` | 该昵称已兑换过此码 | 标记 already，不再重试 |
| `InvalidCode` / `ValidationFailed` / `BadRequest` | 码无效/格式错 | 标记 invalid |
| `ExpiredCode` | 码已过期 | 标记 expired |
| `ExceededUses` | 码达使用上限 | 标记 exceeded |
| `UnavailableCode` | 码暂不可用 | 标记 unavailable |
| `IncorrectUser` | 昵称错误/找不到角色 | 标记 bad_user |
| （网络/5xx） | 临时故障 | 指数退避重试，超上限标 failed |

---

## 二、目录结构

```
bd2-redeem/
├── backend/
│   ├── config.py          # 配置（接口地址、限速 QPS、管理员令牌、抓取源，均可用环境变量覆盖）
│   ├── redeemer.py        # 核心：调用官方接口 + 错误码归一化 + 网络重试
│   ├── db.py              # SQLite：玩家表 / 礼包码表 / 兑换记录关联表（去重核心）
│   ├── worker.py          # 后台限速队列 worker（保护官方服务器，避免封 IP）
│   ├── fetcher.py         # 社区兑换码自动抓取（多源提取 + 伪影归一化 + 去重）
│   ├── scheduler.py 后台定时抓取（见 main.py 内 _fetch_loop）
│   ├── main.py            # FastAPI：绑定 / 录码 / 社区抓取 / 触发 / 统计 + 托管前端
│   ├── run.py             # 启动入口（按 config.HOST/PORT 启动，默认 0.0.0.0 公网可达）
│   └── requirements.txt
├── frontend/
│   ├── index.html         # 玩家端：绑定昵称 + 看生效码（标注来源）+ 查兑换记录
│   └── admin.html         # 管理端：录码 + 立即抓取社区码 + 实时统计 + 玩家/兑换明细
├── redeem_cli.py          # 零依赖命令行工具（仅标准库，可脱离后端直接批量兑换/测试）
├── Dockerfile             # 生产镜像（0.0.0.0 监听）
├── render.yaml            # Render.com 一键部署（免费获得公网域名）
├── Procfile               # PaaS 启动命令
└── data/                  # SQLite 数据库文件（自动生成）
```

---

## 三、快速开始

### 方式 A：完整后端 + 网页（推荐）

```bash
cd bd2-redeem/backend
pip install -r requirements.txt

# 建议先改管理员令牌（否则用默认 change-me-admin-token）
export BD2_ADMIN_TOKEN="你的强口令"      # Windows PowerShell: $env:BD2_ADMIN_TOKEN="..."

python run.py                          # 默认监听 0.0.0.0:8000（公网/局域网可达）
# 或自定义： BD2_HOST=0.0.0.0 BD2_PORT=8000 python run.py
# 仅本机调试可： uvicorn main:app --port 8000
```

打开：
- 玩家端  http://<本机IP或域名>:8000/
- 管理端  http://<本机IP或域名>:8000/admin  （右上角填管理员令牌后「连接/刷新」）

**使用流程**
1. 玩家在首页填 **游戏昵称**（即官方兑换身份）→ 点「立即绑定」（会自动补发所有历史有效码）。绑定有效期 **14 天**，到期前重新绑定可续期。
2. ⚠️ 官方兑换接口 `userId` 认的是**游戏昵称**，不是 UID。系统以玩家填写的游戏昵称为唯一身份调用官方接口，**昵称填错则自动兑换会因 `IncorrectUser` 全部失败**，请务必填写准确可兑换的角色昵称。
3. 管理员在 `/admin` 录入新码 → 系统立刻为所有**有效期内**的已绑定玩家排队，后台按限速自动兑换。
4. **GameKee 自动抓取**：系统**每天**自动从 GameKee 棕色尘埃2 Wiki 接口抓取最新礼包码并入库（入库即自动兑换）；服务启动/唤醒会先立即抓一次；也可在 `/admin` 点「立即抓取社区兑换码」手动触发。
5. 玩家/管理员均可查看每条兑换状态。

#### 🌐 让非本机玩家通过链接访问（公网/局域网）

服务默认监听 `0.0.0.0`（所有网卡），因此有三种方式让别人访问：

1. **局域网（零成本）**：同 Wi-Fi/同一网络下，把 `http://<你的内网IP>:8000/` 发给大家。
   查看本机 IP：`ipconfig`（Windows）/`hostname -I`（Linux）。
2. **内网穿透 / 路由器端口转发**：把 8000 端口映射到公网，或用 Cloudflare Tunnel / 花生壳等。
3. **部署到公网（推荐，获得稳定链接）**：已附 `Dockerfile` + `render.yaml`，推到 GitHub 后在
   [Render.com](https://render.com) 一键部署（免费版即给 `https://<服务名>.onrender.com` 公网域名）。
   务必在部署平台的环境变量里设置 `BD2_ADMIN_TOKEN`。

> 安全提示：玩家绑定接口是公开的（这是设计），但**所有管理接口都要求 `X-Admin-Token`**，
> 请务必设置强口令；公网部署建议再加一层反向代理（Nginx/Caddy）做 HTTPS 与限流。

### 方式 B：零依赖命令行（快速测试，无需安装任何包）

```bash
# 单个（用游戏昵称）
python redeem_cli.py --nickname "你的游戏昵称" --code BD2025SUMMER

# 一个昵称多个码
python redeem_cli.py --nickname "你的游戏昵称" --code CODE1 CODE2 CODE3

# 昵称清单文件批量（players.txt 每行一个昵称）
python redeem_cli.py --nickfile players.txt --code BD2025SUMMER --qps 2.5
```

---

## 四、核心 API

玩家端（公开）
- `POST /api/bind`  绑定昵称 `{"nickname":"游戏昵称", "note":"可选"}`（有效期 14 天）
- `GET  /api/codes`  当前生效礼包码 + 有效玩家总数
- `GET  /api/status/{nickname}`  查询某昵称的兑换记录

管理端（需请求头 `X-Admin-Token`）
- `POST /admin/codes`  录入新码 `{"code":"...", "description":"...", "expires_at":null}` → 自动触发兑换
- `POST /admin/fetch`  立即从社区源抓取礼包码并入库（后台也会定时自动执行）
- `GET  /admin/sources`  查看当前抓取源与开关
- `POST /admin/codes/{code}/deactivate`  去激活某个码（如自动抓到失效/无效码）
- `GET  /admin/stats`  统计
- `GET  /admin/redemptions?limit=100`  兑换明细
- `GET  /admin/players`  玩家列表

### 社区兑换码自动抓取

`fetcher.py` 只从 **GameKee 棕色尘埃2 Wiki** 接口抓取最新码，归一化后入库（入库即触发自动兑换）。

- **唯一源**：`gamekee.com`（zsca2，接口 `GET /v1/game/cdk/queryByServerIdPageList`，需带 `game-alias: zsca2` 头），
  由 Wiki 编辑从官方渠道维护，返回**中文奖励 + 精确过期时间戳**。不再抓取其它英文社区站。
- **识别策略**：GameKee 接口直接给出礼包码与奖励，无需页面正则；保留的通用 HTML 提取函数仅供本地调试/测试。
- **无害兜底**：抓到的候选码若经 worker 实际兑换判定为无效/过期，会被标记且不影响玩家；管理员可在
  `/admin` 一键去激活。建议定期清理过期码以保持列表整洁。
- **抓取频率**：`BD2_FETCH_INTERVAL_MIN`（默认 **1440 分钟 = 每天一轮**）；设 `BD2_FETCH_ENABLED=0` 可关闭。
- **GameKee 专属配置**：`BD2_GAMEKEE_ALIAS`（默认 `zsca2`）、`BD2_GAMEKEE_SERVER_ID`（默认 `12`）、
  `BD2_GAMEKEE_FETCH_ENABLED`（默认 `1`）。

---

## 五、限速与去重（重点）

- **限速**：`worker.py` 单消费者按 `BD2_REDEEM_QPS`（默认 2.5 次/秒）匀速请求，防止过快被官方封 IP。玩家多时只是耗时变长，不会打爆官方。
- **去重**：`redemptions` 表 `UNIQUE(player_id, code_id)`，同一昵称同一码只会有一条记录；成功/已兑换/无效等均为最终态，不会重复兑换。
- **断点续跑**：任务状态持久化在 SQLite，进程重启后 pending 任务自动继续，无需 Redis/Celery。
- **可扩展**：规模很大时，把 `worker.py` 换成 Celery / RQ + Redis 即可，`redeemer.py` 逻辑不变。

## 六、配置项（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `BD2_ADMIN_TOKEN` | `change-me-admin-token` | 管理后台令牌，**务必修改** |
| `BD2_HOST` | `0.0.0.0` | 监听地址（默认所有网卡，公网/局域网可达；本机调试设 `127.0.0.1`） |
| `BD2_PORT` | `8000` | 监听端口 |
| `BD2_REDEEM_QPS` | `2.5` | 每秒兑换请求上限 |
| `BD2_MAX_RETRIES` | `3` | 单次网络错误重试次数 |
| `BD2_DB_PATH` | `data/bd2.db` | 数据库路径 |
| `BD2_FETCH_ENABLED` | `1`（开） | GameKee 自动抓取总开关，设 `0` 关闭 |
| `BD2_FETCH_INTERVAL_MIN` | `1440` | 自动抓取间隔（分钟，默认每天一轮） |
| `BD2_GAMEKEE_ALIAS` | `zsca2` | GameKee 游戏别名 |
| `BD2_GAMEKEE_SERVER_ID` | `12` | GameKee 兑换码 Wiki 条目 ID |
| `BD2_COUPON_SOURCES` | `[]` | **已停用**，当前只抓 GameKee，设置无效 |
| `BD2_API_ENDPOINT` / `BD2_APP_ID` | 见 config | 接口地址/应用 ID（若官方变更时改这里） |

## 七、合规与风险提示

- 仅调用**官方公开**兑换接口，功能等同于官网网页兑换，奖励发放至游戏邮箱。
- 本项目与 NEOWIZ 无隶属关系；请遵守游戏《服务条款》，合理设置频率，勿滥用。
- 若官方更换接口地址 / 增加签名校验（如需 token、加密参数），需重新用 F12 抓包更新 `config.py`。
- 官方接口目前主要面向官服/全球服；个别区服（如某些独立 E 服）可能不通用，属正常现象。

## 八、常见问题排查

### 绑定/兑换时浏览器报 `SyntaxError: Failed to execute 'json' on 'Response': Unexpected end of JSON input`
含义：后端返回了**空响应体**，浏览器 `r.json()` 解析空串失败。本机直连通常正常，多出现在「通过链接/域名/代理远程访问」时，链路层把响应掐断了。已做两层兜底：
1. **后端**：`main.py` 加了全局异常处理器，任何异常都返回合法 JSON（含 `/api/health` 自检接口），绝不再出现空 body。
2. **前端**：先读文本再解析，带 20s 超时；若仍失败会把**真实 HTTP 状态码 + 原始响应片段**显示在页面上，便于定位。

常见根因与对策：
- **部署平台冷启动**（Render 等免费版首次请求被掐断）：点一下没反应就**重试一次**；升级「常驻」实例可根治。
- **反代/网关截断 POST 响应**：Nginx 加 `proxy_request_buffering off;` 并确保 `proxy_pass` 透传 body；确认未对 `/api/` 做额外缓冲。
- **混内容**（HTTPS 页面调 HTTP 接口被浏览器拦截）：保证页面与接口**同域同协议**；用 `https://你的域名/` 访问，不要 `http://IP`。
- **预览/文件托管打开页面**：若是从某个「预览面板/网盘」打开 `index.html`（而非服务器真地址），`/api/` 请求会被代理返回空。请直接用 `http(s)://你的服务器:端口/` 打开。

先访问 `/api/health` 自检：返回 `{"ok":true,...}` 说明链路与后端均正常，问题在别处。
