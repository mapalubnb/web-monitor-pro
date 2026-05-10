# web-monitor-pro

> 🕵️ 高风控友好的网页变化监控服务，以飞书交互机器人为入口。支持 Cloudflare 防护网页、SPA、JSON API，**不依赖 Playwright/浏览器**，资源占用极低。

---

## 核心特性

- 🎯 **多策略抓取引擎**：API 逆向 → `curl_cffi` 伪造 Chrome TLS/JA3 指纹 → `httpx` → `Jina Reader` 兜底，自动 fallback
- 🔎 **精准变化识别**：文本级 diff (`difflib`) + JSON 结构化 diff (`deepdiff`)，精确到行、字段
- 🤖 **飞书深度交互**：WebSocket 长连接（**服务器无需公网端口**）、卡片推送、按钮交互、命令系统
- 🛡️ **风控友好**：TLS 指纹伪装 / 浏览器请求头 / 单域名限流 / 请求抖动 / 指数退避 / 推送冷却 / 相似度阈值过滤
- 📝 **中文日志**：`loguru` 全中文日志，按天轮转，`/log` 命令附完整日志文件下载
- 📦 **一键部署**：`install.sh` + `systemd`，针对 Ubuntu 24.04 LTS 优化

---

## 架构

```
┌───────────────────────────────────────────────────────────┐
│                    web-monitor-pro                        │
├───────────────────────────────────────────────────────────┤
│                                                           │
│   APScheduler ──► FetchEngine ──► Extractor ──► Differ    │
│                        │              │           │        │
│                        ▼              ▼           ▼        │
│      [curl_cffi] [httpx] [jina]  [trafilatura]  [difflib] │
│                                  [selectolax]   [deepdiff]│
│                        │                                  │
│                        ▼                                  │
│                  RiskControl ──► FeishuClient             │
│                                      │                    │
├──────────────────────────────────────┼────────────────────┤
│                                      ▼                    │
│                     飞书开放平台（WebSocket 长连接）         │
└──────────────────────────────────────┬────────────────────┘
                                       │
                                       ▼
                       用户在群 / 私聊里用命令和按钮交互
```

---

## 快速开始

### 1. 克隆并安装

```bash
git clone https://github.com/mapalubnb/web-monitor-pro.git
cd web-monitor-pro
sudo bash install.sh
```

安装脚本会自动：
- 安装系统依赖（Python 3.11+、libcurl 等）
- 创建 `venv` 并安装 Python 依赖
- 初始化 `.env` 和 `config.yaml`
- 注册 systemd 服务

### 2. 配置飞书凭证

```bash
sudo vim .env
```

至少填入：
```ini
FEISHU_APP_ID=cli_xxxxxxxxxx
FEISHU_APP_SECRET=xxxxxxxxxxxx
FEISHU_TARGET_CHAT_ID=oc_xxxxxxxxxx
```

👉 详细步骤见 [docs/feishu-setup.md](docs/feishu-setup.md)

### 3. 启动

```bash
sudo systemctl start web-monitor-pro
sudo journalctl -u web-monitor-pro -f
```

启动后群里会收到 `🚀 Web Monitor Pro 已启动` 的卡片。

### 4. 添加第一个监控

在飞书里 @机器人 发送：

```
/add https://github.com/trending --name GitHub趋势 --interval 300
```

或者针对 SPA 网页（如 four.meme）：

```
/add https://four.meme/en/create-token --strategy curl_cffi --extract-next-data
```

或 API 监控：

```
/add https://api.example.com/v1/items --type json --json-path "data[*].name"
```

---

## 飞书命令速查

在群里 `@机器人 /命令` 或私聊直接发 `/命令`：

| 命令 | 说明 |
| --- | --- |
| `/help` | 查看所有命令 |
| `/add <url> [选项]` | 新增监控任务 |
| `/list` | 列出所有任务 |
| `/pause <id>` `/resume <id>` `/remove <id>` | 任务管理 |
| `/check <id>` | 立即触发检查 |
| `/history <id>` | 查看变更历史 |
| `/keyword <id> add/remove <关键字>` | 自定义关键字过滤 |
| `/config` | 查看全局配置 |
| `/log [--tail N]` | 查看日志（附完整日志下载） |
| `/status` | 服务健康状态 |
| `/mute 30m` / `/unmute` | 临时免打扰 |
| `/sniff <url>` | 抓包助手：引导你找 API |

完整命令说明：[docs/commands.md](docs/commands.md)

---

## 卡片预览

**🚀 启动卡片**
- 任务数 / 默认间隔 / 启动时间 / 版本

**📸 首次快照卡片** （附 `.txt` 文件）
- 任务信息 + [🔍 查看详情] [⏸️ 暂停] [🗑️ 删除] 按钮

**🔔 变更推送卡片** （附 `.diff` 文件）
- `➕ +8 行 / ➖ -2 行` 统计
- 命中的关键字
- 人类可读的 diff 摘要
- [🔗 打开页面] [📜 历史变更] [⏸️ 暂停] [🗑️ 删除] 按钮

**📝 日志卡片** （附完整日志文件）
- 末尾 N 行预览
- 完整日志文件作为附件一并下载

---

## 抓取策略说明

web-monitor-pro 针对不同网页难度有不同策略，见 [docs/fetch-strategies.md](docs/fetch-strategies.md)：

| 等级 | 特征 | 推荐策略 |
| --- | --- | --- |
| L1-L2 静态 | 政府、新闻、博客 | `httpx` |
| L3 Cloudflare | x.com、电商 | `curl_cffi` |
| L4 SPA | four.meme / pump.fun | `curl_cffi --extract-next-data` 或 **API 逆向** ⭐ |
| L5 强动态 | 交易所、WAF | `jina` / `firecrawl` |

**推荐打法**：先用 `/sniff <url>` 找到内部 API，再用 `--type json` 监控——又快又稳又省。

---

## 文档

- 📘 [飞书应用配置指南](docs/feishu-setup.md)
- 🚀 [部署指南](docs/deploy.md)
- 🎯 [抓取策略详解](docs/fetch-strategies.md)
- 📖 [命令完整说明](docs/commands.md)

---

## 风控与优雅

- **TLS/JA3 指纹伪装**：`curl_cffi` 模拟真实 Chrome/Firefox/Safari
- **浏览器请求头**：完整的 `Sec-Fetch-*` / `Sec-Ch-Ua`，User-Agent 轮换
- **单域名限流**：默认 10 秒（可配）
- **请求抖动**：±30%，避免固定周期被识别
- **失败指数退避**：60s → 5min → 15min → 1h
- **推送冷却**：同任务 30 秒内只推一次
- **噪音过滤**：变化占比低于 0.5% 视为噪音
- **连续失败告警**：连续 3 次失败才推送告警（单次抖动不刷屏）

---

## 许可

MIT
