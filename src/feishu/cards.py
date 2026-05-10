"""
飞书卡片模板库

统一的卡片生成函数，外观风格（emoji + 中文 + 良好排版）：
- startup_card        服务启动卡片
- help_card           命令帮助卡片
- first_snapshot_card 首次快照建立卡片
- change_card         变更推送卡片
- task_list_card      任务列表卡片（含操作按钮）
- task_detail_card    任务详情卡片
- history_card        历史变更卡片
- status_card         服务状态卡片
- log_card            日志查看卡片（带下载按钮）
- config_card         配置查看卡片
- error_card          错误提示卡片
- success_card        操作成功卡片
- sniff_helper_card   抓包助手卡片

所有卡片都遵循飞书交互卡片 2.0 schema 格式。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


# ============================================================
# 颜色主题：统一配色
# ============================================================
# 标题栏颜色（飞书支持：blue, wathet, turquoise, green, yellow, orange, red, carmine, violet, purple, indigo, grey）
THEME = {
    "info": "blue",
    "success": "green",
    "warning": "yellow",
    "error": "red",
    "change": "orange",
    "startup": "turquoise",
    "first": "violet",
}


def _card(
    title: str,
    template: str,
    elements: list[dict[str, Any]],
    *,
    subtitle: str = "",
) -> dict[str, Any]:
    """构造一个完整的飞书交互卡片（schema 2.0 简化写法）。"""
    header: dict[str, Any] = {
        "template": template,
        "title": {"tag": "plain_text", "content": title},
    }
    if subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": subtitle}
    return {
        "config": {
            "wide_screen_mode": True,
            "update_multi": True,
        },
        "header": header,
        "elements": elements,
    }


def _div(content: str) -> dict[str, Any]:
    """markdown 段落元素。"""
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _hr() -> dict[str, Any]:
    """分割线。"""
    return {"tag": "hr"}


def _note(content: str) -> dict[str, Any]:
    """灰色小备注。"""
    return {
        "tag": "note",
        "elements": [{"tag": "lark_md", "content": content}],
    }


def _btn(text: str, action: str, task_id: int | None = None, *, style: str = "default",
         url: str | None = None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """按钮元素。action 放在 value 里供回调时识别。"""
    value: dict[str, Any] = {"action": action}
    if task_id is not None:
        value["task_id"] = task_id
    if extra:
        value.update(extra)

    btn: dict[str, Any] = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": style,  # default / primary / danger
        "value": value,
    }
    if url:
        btn["url"] = url
    return btn


def _action(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tag": "action", "actions": buttons}


def _field(label: str, value: str, short: bool = True) -> dict[str, Any]:
    return {"is_short": short, "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"}}


def _fields(pairs: list[tuple[str, str, bool]]) -> dict[str, Any]:
    """多字段排版：[(label, value, is_short)]"""
    return {
        "tag": "div",
        "fields": [_field(l, v, s) for l, v, s in pairs],
    }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 1. 启动卡片
# ============================================================
def startup_card(task_count: int, default_interval: int, version: str = "0.1.0") -> dict[str, Any]:
    elements = [
        _div(
            "🎉 **网页监控服务已成功启动**\n\n"
            "所有风控策略和调度器已就绪，将自动执行监控任务并在检测到变化时推送。"
        ),
        _hr(),
        _fields([
            ("📊 加载任务", f"{task_count} 个", True),
            ("⏱️ 默认间隔", f"{default_interval} 秒", True),
            ("🕐 启动时间", _now(), True),
            ("🔖 服务版本", f"v{version}", True),
        ]),
        _hr(),
        _div("💡 发送 `/help` 查看所有命令，或 `/list` 查看当前任务列表"),
    ]
    return _card("🚀 Web Monitor Pro 已启动", THEME["startup"], elements)


# ============================================================
# 2. 命令帮助卡片
# ============================================================
def help_card() -> dict[str, Any]:
    commands = [
        ("🛠️  任务管理",
         "`/add <url> [选项]` — 新增监控任务\n"
         "`/list` — 列出所有任务\n"
         "`/pause <id>` / `/resume <id>` / `/remove <id>` — 暂停/恢复/删除\n"
         "`/check <id>` — 立即触发一次检查\n"
         "`/history <id>` — 查看任务历史变更\n"
         "`/interval <id> <秒>` — 修改检查间隔"),
        ("🎯  精细化配置",
         "`/keyword <id> add <关键字>` — 添加关键字过滤\n"
         "`/keyword <id> remove <关键字>` — 移除关键字\n"
         "`/sniff <url>` — 抓包助手（引导找 API）\n"
         "`/debug <id>` — 诊断页面（识别框架、找数据点、给建议）"),
        ("📊  服务管理",
         "`/status` — 查看运行状态\n"
         "`/config` — 查看全局配置\n"
         "`/log [--tail N]` — 查看日志（附完整日志下载按钮）\n"
         "`/mute 30m` / `/unmute` — 临时免打扰 / 恢复"),
    ]

    elements: list[dict[str, Any]] = [
        _div("📖 **Web Monitor Pro 命令手册**\n\n以下是所有可用命令，在群里 @机器人 或私聊触发均可。"),
        _hr(),
    ]
    for section, content in commands:
        elements.append(_div(f"**{section}**\n{content}"))
        elements.append(_hr())
    elements.append(_note(
        "💡 示例：`/add https://example.com --name 官网 --interval 60 --keyword 招聘`\n"
        "💡 更多高级选项见 README"
    ))

    return _card("📘 命令帮助", THEME["info"], elements)


# ============================================================
# 3. 首次快照卡片
# ============================================================
def first_snapshot_card(
    task_id: int,
    task_name: str,
    url: str,
    content_length: int,
    strategy: str,
) -> dict[str, Any]:
    elements = [
        _div(
            f"📸 **已成功建立首次快照**\n\n"
            f"任务【{task_name}】已启动监控，基准内容已保存。"
            f"后续检测到变化时将自动推送 diff。"
        ),
        _hr(),
        _fields([
            ("🆔 任务 ID", f"#{task_id}", True),
            ("🎯 抓取策略", f"`{strategy}`", True),
            ("📝 正文长度", f"{content_length:,} 字", True),
            ("🕐 首次时间", _now(), True),
        ]),
        _div(f"🔗 **URL**：{url}"),
        _note("📎 完整的快照内容已作为 txt 文件一并发送，点击下载查看"),
        _hr(),
        _action([
            _btn("🔍 查看详情", "task_detail", task_id),
            _btn("⏸️ 暂停", "pause", task_id),
            _btn("🗑️ 删除", "remove", task_id, style="danger"),
        ]),
    ]
    return _card("✅ 首次快照已建立", THEME["first"], elements, subtitle=task_name)


# ============================================================
# 4. 变更推送卡片
# ============================================================
def change_card(
    task_id: int,
    task_name: str,
    url: str,
    added_count: int,
    removed_count: int,
    change_ratio: float,
    diff_summary: str,
    strategy: str,
    matched_keywords: list[str] | None = None,
    has_diff_file: bool = True,
) -> dict[str, Any]:
    keyword_line = ""
    if matched_keywords:
        keyword_line = f"🎯 **命中关键字**：`{', '.join(matched_keywords)}`\n\n"

    # diff_summary 可能较长，限制在卡片里的展示量
    display_diff = diff_summary
    if len(display_diff) > 2800:
        display_diff = display_diff[:2800] + "\n\n…（完整内容见附件）"

    elements = [
        _div(
            f"🔔 **检测到页面变化**\n\n"
            f"{keyword_line}"
            f"任务【{task_name}】有新变更，请查看下方 diff 详情。"
        ),
        _hr(),
        _fields([
            ("🆔 任务 ID", f"#{task_id}", True),
            ("📊 变化", f"➕ {added_count} 行 / ➖ {removed_count} 行", True),
            ("📈 变化占比", f"{change_ratio:.2%}", True),
            ("🎯 抓取策略", f"`{strategy}`", True),
        ]),
        _hr(),
        _div(f"```diff\n{display_diff or '（diff 摘要为空）'}\n```"),
    ]
    if has_diff_file:
        elements.append(_note("📎 完整 diff 已作为附件发送，点击下载查看"))
    elements.append(_hr())
    elements.append(_action([
        _btn("🔗 打开页面", "open_url", task_id, url=url),
        _btn("🔍 历史变更", "history", task_id),
        _btn("⏸️ 暂停", "pause", task_id),
        _btn("🗑️ 删除", "remove", task_id, style="danger"),
    ]))

    return _card("🔔 页面变化通知", THEME["change"], elements, subtitle=task_name)


# ============================================================
# 5. 任务列表卡片
# ============================================================
def task_list_card(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """
    tasks: 每项含 id/name/url/enabled/interval/total_checks/total_changes/last_checked_at
    """
    if not tasks:
        return _card(
            "📋 监控任务列表",
            THEME["info"],
            [
                _div("（当前没有任务）"),
                _note("💡 发送 `/add <url>` 新增一个监控任务"),
            ],
        )

    elements: list[dict[str, Any]] = [
        _div(f"📋 当前共 **{len(tasks)}** 个监控任务："),
        _hr(),
    ]
    for t in tasks:
        status = "🟢 运行中" if t["enabled"] else "⏸️ 已暂停"
        last_time = t.get("last_checked_at") or "从未"
        elements.append(_div(
            f"**#{t['id']} · {t['name']}**  {status}\n"
            f"🔗 {t['url']}\n"
            f"⏱️ 间隔 `{t['interval']}s` · "
            f"📊 检查 `{t.get('total_checks', 0)}` 次 · "
            f"🔔 变更 `{t.get('total_changes', 0)}` 次 · "
            f"🕐 上次 `{last_time}`"
        ))
        btns = [_btn("⚡ 立即检查", "check", t["id"]),
                _btn("📜 历史", "history", t["id"])]
        if t["enabled"]:
            btns.append(_btn("⏸️ 暂停", "pause", t["id"]))
        else:
            btns.append(_btn("▶️ 恢复", "resume", t["id"], style="primary"))
        btns.append(_btn("🗑️ 删除", "remove", t["id"], style="danger"))
        elements.append(_action(btns))
        elements.append(_hr())

    elements.append(_note("💡 点击按钮进行操作，或发送命令 `/add <url>` 添加新任务"))
    return _card("📋 监控任务列表", THEME["info"], elements)


# ============================================================
# 6. 任务详情卡片
# ============================================================
def task_detail_card(t: dict[str, Any]) -> dict[str, Any]:
    status = "🟢 运行中" if t["enabled"] else "⏸️ 已暂停"
    keywords = ", ".join(t.get("keywords") or []) or "（未配置）"

    elements = [
        _div(f"**{t['name']}**  {status}"),
        _div(f"🔗 **URL**：{t['url']}"),
        _hr(),
        _fields([
            ("🆔 任务 ID", f"#{t['id']}", True),
            ("⏱️ 间隔", f"{t['interval']} 秒", True),
            ("🎯 类型", t.get("type", "html"), True),
            ("🤖 策略", t.get("strategy", "auto"), True),
            ("🎭 模拟浏览器", t.get("impersonate", "chrome131"), True),
            ("📊 检查次数", str(t.get("total_checks", 0)), True),
            ("🔔 变更次数", str(t.get("total_changes", 0)), True),
            ("❌ 连续失败", str(t.get("consecutive_failures", 0)), True),
        ]),
        _div(f"🎯 **关键字过滤**：{keywords}"),
        _div(f"🕐 **上次检查**：{t.get('last_checked_at') or '从未'}"),
        _div(f"🔔 **上次变更**：{t.get('last_changed_at') or '从未'}"),
        _hr(),
        _action([
            _btn("⚡ 立即检查", "check", t["id"], style="primary"),
            _btn("📜 历史变更", "history", t["id"]),
            _btn("⏸️ 暂停" if t["enabled"] else "▶️ 恢复",
                 "pause" if t["enabled"] else "resume", t["id"]),
            _btn("🗑️ 删除", "remove", t["id"], style="danger"),
        ]),
    ]
    return _card("📄 任务详情", THEME["info"], elements, subtitle=f"#{t['id']} · {t['name']}")


# ============================================================
# 7. 历史变更卡片
# ============================================================
def history_card(task_name: str, task_id: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return _card(
            "📜 历史变更",
            THEME["info"],
            [_div(f"**{task_name}** #{task_id}"), _note("（暂无历史变更记录）")],
        )

    elements: list[dict[str, Any]] = [
        _div(f"📜 **{task_name}** #{task_id} 的最近变更："),
        _hr(),
    ]
    for item in items:
        matched = ", ".join(item.get("matched_keywords") or []) or "无"
        elements.append(_div(
            f"🕐 `{item['created_at']}` · "
            f"➕{item['added_lines']} / ➖{item['removed_lines']} · "
            f"📈{item['change_ratio']:.2%} · "
            f"🎯 {matched}"
        ))
    return _card("📜 历史变更", THEME["info"], elements, subtitle=task_name)


# ============================================================
# 8. 服务状态卡片
# ============================================================
def status_card(data: dict[str, Any]) -> dict[str, Any]:
    elements = [
        _div("💚 **服务健康状态**"),
        _hr(),
        _fields([
            ("⏱️ 运行时长", data.get("uptime", "-"), True),
            ("📋 任务总数", f"{data.get('total_tasks', 0)} ({data.get('active_tasks', 0)} 活跃)", True),
            ("🔔 今日推送", str(data.get("pushes_today", 0)), True),
            ("⚡ 今日检查", str(data.get("checks_today", 0)), True),
            ("❌ 今日错误", str(data.get("errors_today", 0)), True),
            ("🔇 免打扰", data.get("mute_status", "否"), True),
        ]),
        _hr(),
        _fields([
            ("💻 主机", data.get("hostname", "-"), True),
            ("🐍 Python", data.get("python_version", "-"), True),
            ("🔖 版本", f"v{data.get('version', '-')}", True),
            ("📊 内存", data.get("memory", "-"), True),
        ]),
    ]
    return _card("📊 服务状态", THEME["success"], elements)


# ============================================================
# 9. 日志查看卡片
# ============================================================
def log_card(tail_text: str, log_size: str = "", date: str = "") -> dict[str, Any]:
    display = tail_text
    if len(display) > 3800:
        display = "…\n" + display[-3800:]

    subtitle = f"{date} · {log_size}" if (date or log_size) else ""

    elements = [
        _div(f"📝 **当日日志（末尾部分）**"),
        _div(f"```\n{display}\n```"),
        _hr(),
        _note("📎 完整日志已作为附件发送（按钮或文件消息）"),
    ]
    return _card("📝 日志查看", THEME["info"], elements, subtitle=subtitle)


# ============================================================
# 10. 配置查看卡片
# ============================================================
def config_card(cfg: dict[str, Any]) -> dict[str, Any]:
    elements = [
        _div("⚙️ **全局配置概览**"),
        _hr(),
        _fields([
            ("⏱️ 默认间隔", f"{cfg.get('default_check_interval', '-')}s", True),
            ("🚀 最大并发", str(cfg.get("max_concurrent_fetch", "-")), True),
            ("⏰ 域名限流", f"{cfg.get('domain_min_interval', '-')}s", True),
            ("⌛ 请求超时", f"{cfg.get('request_timeout', '-')}s", True),
            ("🌊 抖动比例", f"{cfg.get('jitter_ratio', 0):.1%}", True),
            ("📉 噪音阈值", f"{cfg.get('min_change_ratio', 0):.2%}", True),
            ("❄️ 推送冷却", f"{cfg.get('push_cooldown_seconds', '-')}s", True),
            ("🔔 失败告警", f"连续 {cfg.get('alert_after_consecutive_failures', '-')} 次", True),
        ]),
        _hr(),
        _div(
            f"**🎭 代理**：{cfg.get('proxy_info', '未启用')}\n"
            f"**☁️ 外部 API**：{cfg.get('external_apis', '未启用')}"
        ),
    ]
    return _card("⚙️ 全局配置", THEME["info"], elements)


# ============================================================
# 11. 错误 / 成功 / 告警
# ============================================================
def error_card(title: str, reason: str, suggestion: str = "") -> dict[str, Any]:
    content = f"❌ **{title}**\n\n{reason}"
    if suggestion:
        content += f"\n\n💡 建议：{suggestion}"
    return _card("⚠️ 操作失败", THEME["error"], [_div(content)])


def success_card(title: str, detail: str = "") -> dict[str, Any]:
    content = f"✅ **{title}**"
    if detail:
        content += f"\n\n{detail}"
    return _card("✨ 操作成功", THEME["success"], [_div(content)])


def fetch_failure_card(task_id: int, task_name: str, url: str,
                      consecutive_failures: int, error: str) -> dict[str, Any]:
    elements = [
        _div(
            f"🚨 **抓取连续失败告警**\n\n"
            f"任务【{task_name}】已连续 **{consecutive_failures}** 次抓取失败。"
        ),
        _hr(),
        _fields([
            ("🆔 任务 ID", f"#{task_id}", True),
            ("❌ 连续失败", f"{consecutive_failures} 次", True),
        ]),
        _div(f"🔗 **URL**：{url}"),
        _div(f"```\n{error[:500]}\n```"),
        _hr(),
        _note("💡 建议：尝试更换抓取策略（/add --strategy curl_cffi）或检查目标站点可达性"),
        _action([
            _btn("⚡ 立即重试", "check", task_id, style="primary"),
            _btn("⏸️ 暂停", "pause", task_id),
        ]),
    ]
    return _card("🚨 抓取失败告警", THEME["warning"], elements, subtitle=task_name)


# ============================================================
# 12. 抓包助手
# ============================================================
def sniff_helper_card(url: str) -> dict[str, Any]:
    elements = [
        _div(
            f"🔍 **抓包助手**\n\n"
            f"对于动态加载（SPA）的网站，**直接请求它们的内部 API 比渲染页面更稳定、更快**。\n\n"
            f"目标网站：`{url}`"
        ),
        _hr(),
        _div(
            "**📖 操作步骤：**\n\n"
            "1️⃣ 在 Chrome 打开上面这个网址\n"
            "2️⃣ 按 `F12` 打开开发者工具 → 切到 **Network** 面板 → 筛选 **Fetch/XHR**\n"
            "3️⃣ 刷新页面，观察列表中哪个请求返回了你关心的数据\n"
            "4️⃣ 右键该请求 → Copy → **Copy as cURL** 发给我，或直接贴 API URL"
        ),
        _hr(),
        _div(
            "**💡 识别 API 的小技巧：**\n"
            "• 响应类型是 `application/json`\n"
            "• URL 含 `/api/` `/v1/` 等路径\n"
            "• 预览窗口能看到你关心的数据字段"
        ),
        _note("找到 API 后，用 `/add <api_url> --type json --json-path data[*].name` 即可创建 JSON 监控"),
    ]
    return _card("🔍 抓包助手", THEME["info"], elements)


# ============================================================
# 对外导出
# ============================================================
__all__ = [
    "startup_card",
    "help_card",
    "first_snapshot_card",
    "change_card",
    "task_list_card",
    "task_detail_card",
    "history_card",
    "status_card",
    "log_card",
    "config_card",
    "error_card",
    "success_card",
    "fetch_failure_card",
    "sniff_helper_card",
]
