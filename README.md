# web-monitor-pro

高风控友好的网页变化监控服务，以飞书交互机器人为入口。

支持 Cloudflare 防护网页、SPA（Next.js / Nuxt / React）、JSON API、纯 CSR 站点。全自建无外部 API 依赖。

---

## 架构

```
                    ┌─────────────────────────────────┐
                    │         APScheduler              │
                    │   (interval / on-demand trigger) │
                    └──────────┬──────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────┐
│                      MonitorRunner                           │
│  fetch ──► extract ──► hash ──► diff ──► risk gate ──► push │
└──┬────────────┬──────────────────────┬───────────────────┬──┘
   │            │                      │                   │
   ▼            ▼                      ▼                   ▼
FetchEngine  Extractor             DiffEngine        FeishuClient
 ├ curl_cffi   ├ CSS selector       ├ text diff       ├ WebSocket
 ├ httpx       ├ SPA/RSC data       └ JSON diff       ├ cards
 ├ deep SSR    ├ trafilatura                          └ file upload
 ├ Scrapling   ├ adaptive selector
 └ Playwright  └ innerText
       │
       ▼
  RiskController
   ├ domain throttle
   ├ concurrency semaphore
   ├ push cooldown
   └ noise filter
```

### 抓取策略（自动递进）

| 级别 | 策略 | 适用场景 | 耗时 |
|------|------|---------|------|
| L1 | `httpx` | 静态页面（政府、新闻、博客） | <1s |
| L2 | `curl_cffi` | Cloudflare 防护站点（TLS/JA3 指纹伪装） | 1-3s |
| L3 | Deep Extract | SPA 嵌入数据（Next.js `__NEXT_DATA__` / RSC Flight / Nuxt / Apollo） | <0.1s |
| L4 | `Scrapling` | 自适应选择器、动态页面、隐身抓取增强 | 1-20s |
| L5 | `Playwright` | 纯 CSR / DeFi 前端（headless Chromium 渲染） | 10-20s |

`auto` 模式下自动从 L1 逐级尝试，首次成功后锁定策略（后续复用，失效再降级）。
配置 `--selector` 时会自动启用 Scrapling 自适应重定位，页面小改版时更不容易空提取。

### 变化检测

- **内容归一化**：移除 CSRF token、nonce、buildId、时间戳等动态噪音后再 hash
- **二次确认**：首次检测到变化写 pending，下次再确认相同才推送（防 SPA 渲染抖动）
- **策略一致性**：pending 阶段检查策略是否一致，不同策略的提取结果不可比
- **基准保护**：策略切换时静默更新基准，避免假 diff

---

## 快速开始

### 环境要求

- **Python** >= 3.11
- **OS**: Ubuntu 24.04 LTS（推荐）
- **飞书**：企业自建应用（需 WebSocket 长连接权限）

### 安装

```bash
git clone https://github.com/mapalubnb/web-monitor-pro.git
cd web-monitor-pro
sudo bash install.sh
```

`install.sh` 自动完成：系统依赖安装 → venv 创建 → pip install → Playwright Chromium 安装 → systemd 服务注册。

### 配置

```bash
cp .env.example .env
vim .env
```

必填项：

```ini
FEISHU_APP_ID=cli_xxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxx
FEISHU_TARGET_CHAT_ID=oc_xxxxxxxxxx
```

飞书应用配置详见 [docs/feishu-setup.md](docs/feishu-setup.md)。

可选业务配置 `config.yaml`（风控参数、种子任务等）参照 `config.example.yaml`。

### 启动

```bash
sudo systemctl start web-monitor-pro
sudo journalctl -u web-monitor-pro -f
```

---

## 飞书命令

| 命令 | 说明 |
|------|------|
| `/help` | 查看命令手册 |
| `/add <url> [常用选项]` | 新增监控（常用：`--name` `--interval` `--type json` `--selector` `--json-path` `--keyword`） |
| `/list` | 列出所有任务（带管理按钮） |
| `/check <id>` | 立即触发检查 |
| `/pause <id>` / `/resume <id>` | 暂停 / 恢复 |
| `/remove <id>` | 删除任务 |
| `/keyword <id> add/remove/list/clear <kw>` | 关键字过滤管理（支持逗号/顿号批量） |
| `/interval <id> <seconds>` | 修改检查间隔（≥10 秒） |
| `/debug <id>` | 诊断（识别框架、嵌入数据、给出建议，附 HTML 下载） |
| `/strategy <id> <策略>` | 切换抓取策略：`auto`、`stealth`、`browser`、`fast`、`http`、`scrapling` |
| `/mute <30m/2h/1d>` / `/unmute` | 免打扰 |
| `/log [--tail N]` | 查看日志（附完整日志下载） |
| `/status` | 服务健康状态和配置摘要 |

完整说明：[docs/commands.md](docs/commands.md)

变更历史和快照下载在 `/list` 返回的任务卡片按钮中操作。

常用策略示例：

```text
/strategy 3 auto      # 自动选择，默认推荐
/strategy 3 stealth   # 隐身浏览器，实际策略 scrapling_stealth
/strategy 3 browser   # 普通浏览器，实际策略 playwright
/strategy 3 fast      # 快速模式，实际策略 curl_cffi
/strategy 3 http      # 普通 HTTP，实际策略 httpx
/strategy 3 scrapling # 增强静态抓取，实际策略 scrapling_static
```

---

## 依赖

| 分类 | 库 | 用途 |
|------|-----|------|
| HTTP | `curl_cffi` | TLS/JA3 指纹伪装，过 Cloudflare |
| HTTP | `httpx[http2]` | 轻量 HTTP/2 客户端 |
| 解压 | `brotli` `zstandard` | br/zstd 响应解压 |
| 解析 | `selectolax` | 高性能 HTML 解析 |
| 提取 | `trafilatura` | 正文提取 |
| Diff | `deepdiff` | JSON 结构化 diff |
| 飞书 | `lark-oapi` | 官方 SDK（WebSocket + 消息 API） |
| 调度 | `APScheduler` | 后台任务调度 |
| 存储 | `SQLAlchemy` | ORM + SQLite |
| 配置 | `PyYAML` `python-dotenv` | YAML / .env 解析 |
| 日志 | `loguru` | 中文日志，按天轮转 |
| 渲染 | `playwright` `playwright-stealth` | 无头浏览器（纯 CSR 兜底） |
| 增强 | `scrapling[fetchers]` | 自适应选择器 + 隐身抓取 |

---

## 风控策略

- **TLS/JA3 指纹伪装**：`curl_cffi` 模拟 Chrome/Firefox/Safari 握手特征
- **浏览器请求头**：完整 `Sec-Fetch-*` / UA 轮换 / `Accept-Encoding` 自适应
- **单域名限流**：默认 10s 间隔（±30% 抖动，避免固定周期）
- **并发控制**：信号量限制最大并发抓取数（默认 5）
- **可选免费代理池**：支持 Proxifly `free-proxy-list`，`.env` 模板默认启用，可自动拉取/缓存/轮换
- **失败退避**：60s → 5min → 15min → 1h 阶梯退避
- **熔断**：连续失败达到阈值（默认 20，可通过 `CIRCUIT_BREAKER_THRESHOLD` 配置）后自动禁用任务并告警
- **推送冷却**：同任务 30s 内最多推一次
- **噪音过滤**：变化占比 < 0.5% 视为噪音不推送
- **Playwright stealth**：隐藏无头浏览器特征
- **Scrapling 自适应选择器**：配置 `--selector` 后自动保存元素特征，选择器失效时尝试重定位

---

## 项目结构

```
src/
├── main.py              # 入口：初始化 + 飞书 WebSocket 事件循环
├── config.py            # 配置加载（.env + config.yaml）
├── db.py                # SQLAlchemy ORM（tasks / change_history / push_log）
├── logger.py            # loguru 日志（按天轮转）
├── scheduler.py         # APScheduler 封装
├── risk_control.py      # 风控（抓取限流 + 推送过滤）
├── fetcher/
│   ├── engine.py        # 四级递进抓取引擎
│   └── extractor.py     # 内容提取 + 归一化
├── differ/
│   └── text_diff.py     # 文本/JSON diff + 关键字过滤
├── tasks/
│   └── monitor_task.py  # 单任务执行闭环（二次确认 + 策略锁定）
└── feishu/
    ├── client.py        # 飞书 SDK 封装
    ├── commands.py       # 命令分发
    └── cards.py          # 卡片模板
```

---

## 文档

- [飞书应用配置指南](docs/feishu-setup.md)
- [部署指南](docs/deploy.md)
- [抓取策略详解](docs/fetch-strategies.md)
- [命令完整说明](docs/commands.md)

## 许可

MIT
