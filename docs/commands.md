# 命令完整说明

在飞书群里 **@机器人 /命令** 或 私聊机器人 发送 `/命令` 即可触发。
所有命令都会以卡片形式返回结果。

---

## 任务管理

### `/add <url> [选项]`

新增一个监控任务。

| 选项 | 说明 | 默认 |
| --- | --- | --- |
| `--name "名字"` | 任务名称 | URL 的域名+路径 |
| `--interval 60` | 检查间隔（秒） | `DEFAULT_CHECK_INTERVAL` |
| `--type html\|json` | 内容类型 | `html` |
| `--strategy auto\|httpx\|curl_cffi\|playwright\|scrapling_static\|scrapling_dynamic\|scrapling_stealth\|scrapling_auto` | 抓取策略 | `auto` |
| `--impersonate chrome131\|chrome124\|firefox133\|safari18_0` | curl_cffi 模拟的浏览器 | `chrome131` |
| `--selector "article"` | CSS 选择器（仅 html） | 自动抽正文 |
| `--adaptive-selector` | 对 CSS 选择器启用 Scrapling 自适应重定位 | 配置 selector 时自动开启 |
| `--no-adaptive-selector` | 关闭 selector 自适应重定位 | 关闭 |
| `--selector-id main` | 自适应选择器存储标识 | 选择器本身 |
| `--adaptive-threshold 40` | 自适应重定位最低相似度 | `40` |
| `--wait-selector "main"` | 浏览器策略等待指定元素出现 | 动态策略下自动复用 selector |
| `--json-path "data[*].name"` | JSON 字段路径（仅 json） | 整个 JSON |
| `--extract-next-data` | 提取 Next.js `__NEXT_DATA__` | 关闭 |
| `--keyword "招聘" --keyword "实习"` | 关键字过滤（命中才推送，可多个） | 空 |

**示例：**

```
/add https://github.com/trending --name GitHub趋势 --interval 300 --selector "article.Box-row"
```

```
/add https://example.com/news
```

```
/add https://example.com/news --selector "main"
```

```
/add https://four.meme/en/create-token --extract-next-data
```

```
/add https://api.example.com/v1/items --type json --json-path "data[*].name,data[*].price"
```

高级兜底：

```
/strategy <id> stealth
```

---

### `/list`

列出所有监控任务（带操作按钮：立即检查 / 历史 / 暂停 / 删除）

---

### `/pause <id>` / `/resume <id>` / `/remove <id>`

暂停 / 恢复 / 删除指定任务。

---

### `/check <id>`

立即触发一次检查，不等定时器。

---

### `/history <id>`

查看该任务最近 10 次变更记录。

---

### `/snapshot <id>`

下载该任务当前基准快照。

---

### `/strategy <id> <策略>`

给任务切换抓取策略，并清空旧基准快照。切换后会立即触发一次抓取，用新策略重新建立基准。

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
```

---

### `/reset <id> [选项]`

清空基准快照，下次检查会重新建立首次快照。可同时调整高级参数：

| 选项 | 说明 |
| --- | --- |
| `--strategy scrapling_stealth` | 切换抓取策略（新手建议优先用 `/strategy <id> stealth`） |
| `--impersonate chrome131` | 切换 curl_cffi 浏览器指纹 |
| `--selector "main"` | 更新 CSS 选择器，并默认启用自适应 |
| `--no-adaptive-selector` | 关闭 selector 自适应 |
| `--wait-selector "main"` | 浏览器策略等待指定元素 |
| `--extract-next-data` | 启用 SPA 嵌入数据提取 |

---

### `/keyword <id> add <关键字>` / `/keyword <id> remove <关键字>`

为任务添加或移除关键字。设置关键字后，**只有 diff 命中关键字才会推送**。

```
/keyword 3 add 招聘
/keyword 3 add iPhone
```

---

## 服务管理

### `/status`

查看服务健康状态：运行时长、任务数、今日推送数、今日检查数、错误数、内存占用等。

---

### `/config`

查看全局配置概览（不含敏感凭证）。

---

### `/log [--tail N]`

查看当日日志末尾 N 行，**同时附上完整日志文件作为下载附件**。

```
/log              # 默认末尾 100 行
/log --tail 500   # 末尾 500 行
```

---

### `/mute <时长>` / `/unmute`

临时免打扰 / 取消免打扰。期间检测到的变更不会推送，但仍会正常检查并记录。

```
/mute 30m    # 免打扰 30 分钟
/mute 2h     # 免打扰 2 小时
/mute 1d     # 免打扰 1 天
```

---

### `/sniff <url>`

**抓包助手**：当目标是动态加载（SPA）的网站时，直接请求它的内部 API 比渲染页面更稳定。
此命令会返回一份操作指南，教你用 Chrome F12 找到 API。

```
/sniff https://four.meme/en/create-token
```

---

### `/debug <id>`

立即抓取一次并诊断页面：识别框架、嵌入数据、当前策略和修复建议，同时附上 HTML 文件。

---

### `/help`

显示所有命令（卡片形式）。

---

## 卡片按钮

所有返回的卡片中的按钮（⚡ 立即检查 / ⏸️ 暂停 / ▶️ 恢复 / 🗑️ 删除 / 📜 历史等）
**功能等价于对应的命令**，点击即可触发。
