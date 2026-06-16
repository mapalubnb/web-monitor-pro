# 飞书命令

在飞书群里 `@机器人 /命令`，或私聊机器人发送 `/命令` 即可触发。命令会返回卡片，任务列表卡片里提供历史、快照、检查、暂停、删除等按钮。

## 常用命令

| 命令 | 用途 | 示例 |
| --- | --- | --- |
| `/help` | 查看命令手册 | `/help` |
| `/add <url>` | 新增监控任务 | `/add https://example.com/news` |
| `/list` | 查看任务和按钮操作 | `/list` |
| `/check <id>` | 立即检查一次 | `/check 3` |
| `/pause <id>` | 暂停任务 | `/pause 3` |
| `/resume <id>` | 恢复任务 | `/resume 3` |
| `/remove <id>` | 删除任务 | `/remove 3` |
| `/debug <id>` | 抓取诊断和修复建议 | `/debug 3` |
| `/log [--tail N]` | 查看日志并附完整日志文件 | `/log --tail 200` |
| `/status` | 服务状态和配置摘要 | `/status` |
| `/mute <时长>` | 临时免打扰 | `/mute 30m` |
| `/unmute` | 关闭免打扰 | `/unmute` |

## 新增任务

最简单只需要 URL：

```text
/add https://example.com/news
```

常用选项：

| 选项 | 用途 | 示例 |
| --- | --- | --- |
| `--name` | 设置任务名称 | `--name GitHub趋势` |
| `--interval` | 设置检查间隔，单位秒 | `--interval 300` |
| `--selector` | 只监控指定 CSS 区域 | `--selector "main"` |
| `--type json` | 监控 JSON/API | `--type json` |
| `--json-path` | 只监控 JSON 指定字段 | `--json-path "data[*].name"` |
| `--keyword` | 只推送命中关键字的变化 | `--keyword 招聘` |

示例：

```text
/add https://github.com/trending --name GitHub趋势 --interval 300 --selector "article.Box-row"
/add https://api.example.com/v1/items --type json --json-path "data[*].name,data[*].price"
```

## 调整任务

### 检查间隔

```text
/interval 3 300
```

### 关键字

```text
/keyword 3 add 招聘
/keyword 3 add 招聘,实习,Python
/keyword 3 remove 招聘
/keyword 3 list
/keyword 3 clear
```

### 抓取策略

`/strategy` 会清空旧基准，并立即用新策略重新抓取一次。

| 策略 | 实际策略值 | 适用场景 |
| --- | --- | --- |
| `auto` | `auto` | 自动选择，默认推荐 |
| `stealth` | `scrapling_stealth` | 风控、Cookie 同意页、纯动态页面 |
| `browser` | `playwright` | 普通浏览器渲染，适合 JS 页面 |
| `fast` | `curl_cffi` | 快速抓取，适合 Cloudflare/TLS 指纹页面 |
| `http` | `httpx` | 普通静态页面 |
| `scrapling` | `scrapling_static` | 增强静态抓取，适合 HTML/选择器自适应 |

示例：

```text
/strategy 3 stealth
/strategy 3 browser
/strategy 3 auto
/strategy 3 scrapling
```

## 按钮操作

`/list` 返回的任务卡片包含按钮：

| 按钮 | 用途 |
| --- | --- |
| 打开 | 打开目标页面 |
| 检查 | 立即检查一次 |
| 详情 | 查看任务配置和状态 |
| 快照 | 下载当前基准快照 |
| 历史 | 查看最近 10 条变更 |
| 暂停 / 恢复 | 切换任务状态 |
| 删除 | 删除任务 |

## 旧命令迁移

以下文本命令已收敛，不再作为常用入口：

| 旧命令 | 新入口 |
| --- | --- |
| `/history <id>` | `/list` 卡片里的「历史」按钮 |
| `/snapshot <id>` | `/list` 卡片里的「快照」按钮 |
| `/config` | `/status` |
| `/sniff <url>` | `/debug <id>` |
| `/reset <id> ...` | `/strategy <id> <策略>` 或重新 `/add` |
| `/delete <id>` | `/remove <id>` |
| `/logs` | `/log` |
