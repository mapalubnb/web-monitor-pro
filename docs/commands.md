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
| `--strategy auto\|httpx\|curl_cffi\|playwright` | 抓取策略 | `auto` |
| `--impersonate chrome131\|chrome124\|firefox133\|safari18_0` | curl_cffi 模拟的浏览器 | `chrome131` |
| `--selector "article"` | CSS 选择器（仅 html） | 自动抽正文 |
| `--json-path "data[*].name"` | JSON 字段路径（仅 json） | 整个 JSON |
| `--extract-next-data` | 提取 Next.js `__NEXT_DATA__` | 关闭 |
| `--keyword "招聘" --keyword "实习"` | 关键字过滤（命中才推送，可多个） | 空 |

**示例：**

```
/add https://github.com/trending --name GitHub趋势 --interval 300 --selector "article.Box-row"
```

```
/add https://four.meme/en/create-token --strategy curl_cffi --extract-next-data
```

```
/add https://pfund.tech/ --strategy playwright --name PFund
```

```
/add https://api.example.com/v1/items --type json --json-path "data[*].name,data[*].price"
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

### `/help`

显示所有命令（卡片形式）。

---

## 卡片按钮

所有返回的卡片中的按钮（⚡ 立即检查 / ⏸️ 暂停 / ▶️ 恢复 / 🗑️ 删除 / 📜 历史等）
**功能等价于对应的命令**，点击即可触发。
