"""
飞书卡片模板库（schema 2.0）

统一的卡片生成函数，外观风格：emoji + 中文 + 良好排版
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# 标题栏颜色
THEME = {
    "info": "blue", "success": "green", "warning": "yellow", "error": "red",
    "change": "orange", "startup": "turquoise", "first": "violet",
}


# ============================================================
# 基础元素 helper
# ============================================================
def _card(title: str, template: str, elements: list[dict],
          subtitle: str = "") -> dict:
    header: dict[str, Any] = {
        "template": template,
        "title": {"tag": "plain_text", "content": title},
    }
    if subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": subtitle}
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": header,
        "elements": elements,
    }


def _div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _hr() -> dict:
    return {"tag": "hr"}


def _note(content: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "lark_md", "content": content}]}


def _btn(text: str, action: str, task_id: int | None = None, *,
         style: str = "default", url: str | None = None) -> dict:
    value: dict[str, Any] = {"action": action}
    if task_id is not None:
        value["task_id"] = task_id
    btn: dict[str, Any] = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": style,
        "value": value,
    }
    if url:
        btn["url"] = url
    return btn


def _action(buttons: list[dict]) -> dict:
    return {"tag": "action", "actions": buttons}


def _fields(pairs: list[tuple[str, str, bool]]) -> dict:
    """字段排版：[(label, value, is_short)]"""
    return {
        "tag": "div",
        "fields": [
            {"is_short": s, "text": {"tag": "lark_md",
                                     "content": f"**{l}**\n{v}"}}
            for l, v, s in pairs
        ],
    }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _task_buttons(task_id: int, enabled: bool, url: str | None = None) -> list[dict]:
    """任务通用的按钮行（复用）。"""
    btns: list[dict] = []
    if url:
        btns.append(_btn("🔗 打开页面", "open_url", task_id, url=url))
    btns.append(_btn("⚡ 立即检查", "check", task_id))
    btns.append(_btn("📜 历史", "history", task_id))
    if enabled:
        btns.append(_btn("⏸️ 暂停", "pause", task_id))
    else:
        btns.append(_btn("▶️ 恢复", "resume", task_id, style="primary"))
    btns.append(_btn("🗑️ 删除", "remove", task_id, style="danger"))
    return btns


# ============================================================
# 1. 启动卡片
# ============================================================
def startup_card(task_count: int, default_interval: int,
                 version: str = "0.1.0") -> dict:
    return _card(
        "🚀 Web Monitor Pro 已启动", THEME["startup"],
        [
            _div("🎉 **网页监控服务已成功启动**\n\n"
                 "所有风控策略和调度器已就绪。"),
            _hr(),
            _fields([
                ("📊 加载任务", f"{task_count} 个", True),
                ("⏱️ 默认间隔", f"{default_interval} 秒", True),
                ("🕐 启动时间", _now(), True),
                ("🔖 版本", f"v{version}", True),
            ]),
            _hr(),
            _div("💡 发送 `/help` 查看命令，`/list` 查看任务列表"),
        ],
    )


# ============================================================
# 2. 命令帮助
# ============================================================
def help_card() -> dict:
    sections = [
        ("🛠️  任务管理",
         "`/add <url> [选项]` — 新增监控\n"
         "`/list` — 列出所有任务\n"
         "`/pause <id>` / `/resume <id>` / `/remove <id>` — 管理任务\n"
         "`/check <id>` — 立即检查\n"
         "`/history <id>` — 查看变更历史\n"
         "`/interval <id> <秒>` — 修改间隔\n"
         "`/reset <id> [--strategy jina]` — 重置基准（策略调优用）"),
        ("🎯  精细化配置",
         "`/keyword <id> add <关键字>` — 添加关键字过滤\n"
         "`/keyword <id> remove <关键字>` — 移除关键字\n"
         "`/sniff <url>` — 抓包助手（引导找 API）\n"
         "`/debug <id>` — 诊断页面（框架/数据点/建议）"),
        ("📊  服务管理",
         "`/status` — 服务健康状态\n"
         "`/config` — 全局配置\n"
         "`/log [--tail N]` — 查看日志\n"
         "`/mute 30m` / `/unmute` — 免打扰"),
    ]
    elements: list[dict] = [
        _div("📖 **Web Monitor Pro 命令手册**\n\n"
             "在群里 @机器人 或私聊发送命令均可。"),
        _hr(),
    ]
    for title, content in sections:
        elements.append(_div(f"**{title}**\n{content}"))
        elements.append(_hr())
    elements.append(_note(
        "💡 `/add https://example.com --name 官网 --interval 60 --keyword 招聘`"
    ))
    return _card("📘 命令帮助", THEME["info"], elements)


# ============================================================
# 3. 首次快照
# ============================================================
def first_snapshot_card(task_id: int, task_name: str, url: str,
                        content_length: int, strategy: str) -> dict:
    return _card(
        "✅ 首次快照已建立", THEME["first"],
        [
            _div(f"📸 **已成功建立首次快照**\n\n"
                 f"任务【{task_name}】已启动监控，基准内容已保存。"),
            _hr(),
            _fields([
                ("🆔 任务 ID", f"#{task_id}", True),
                ("🎯 策略", f"`{strategy}`", True),
                ("📝 正文长度", f"{content_length:,} 字", True),
                ("🕐 时间", _now(), True),
            ]),
            _div(f"🔗 **URL**：{url}"),
            _note("📎 完整快照已附在 txt 文件中"),
            _hr(),
            _action(_task_buttons(task_id, enabled=True, url=url)),
        ],
        subtitle=task_name,
    )


# ============================================================
# 4. 变更推送
# ============================================================
def change_card(task_id: int, task_name: str, url: str,
                added_count: int, removed_count: int,
                change_ratio: float, diff_summary: str, strategy: str,
                matched_keywords: list[str] | None = None,
                has_diff_file: bool = True) -> dict:
    kw_line = (
        f"🎯 **命中关键字**：`{', '.join(matched_keywords)}`\n\n"
        if matched_keywords else ""
    )
    display = diff_summary or "（diff 摘要为空）"
    if len(display) > 2800:
        display = display[:2800] + "\n\n…（完整内容见附件）"

    elements = [
        _div(f"🔔 **检测到页面变化**\n\n{kw_line}"
             f"任务【{task_name}】有新变更。"),
        _hr(),
        _fields([
            ("🆔 任务 ID", f"#{task_id}", True),
            ("📊 变化", f"➕ {added_count} / ➖ {removed_count} 行", True),
            ("📈 占比", f"{change_ratio:.2%}", True),
            ("🎯 策略", f"`{strategy}`", True),
        ]),
        _hr(),
        _div(f"```diff\n{display}\n```"),
    ]
    if has_diff_file:
        elements.append(_note("📎 完整 diff 已作为附件发送"))
    elements.append(_hr())
    elements.append(_action(_task_buttons(task_id, enabled=True, url=url)))

    return _card("🔔 页面变化通知", THEME["change"], elements, subtitle=task_name)


# ============================================================
# 5. 任务列表
# ============================================================
def task_list_card(tasks: list[dict]) -> dict:
    if not tasks:
        return _card(
            "📋 监控任务列表", THEME["info"],
            [_div("（当前没有任务）"),
             _note("💡 发送 `/add <url>` 新增任务")],
        )

    elements: list[dict] = [
        _div(f"📋 当前共 **{len(tasks)}** 个监控任务："),
        _hr(),
    ]
    for t in tasks:
        status = "🟢 运行中" if t["enabled"] else "⏸️ 已暂停"
        last = t.get("last_checked_at") or "从未"
        elements.append(_div(
            f"**#{t['id']} · {t['name']}**  {status}\n"
            f"🔗 {t['url']}\n"
            f"⏱️ `{t['interval']}s` · "
            f"📊 检查 `{t.get('total_checks', 0)}` · "
            f"🔔 变更 `{t.get('total_changes', 0)}` · "
            f"🕐 `{last}`"
        ))
        elements.append(_action(_task_buttons(t["id"], t["enabled"])))
        elements.append(_hr())

    elements.append(_note("💡 点按钮操作，或 `/add <url>` 添加新任务"))
    return _card("📋 监控任务列表", THEME["info"], elements)


# ============================================================
# 6. 任务详情
# ============================================================
def task_detail_card(t: dict) -> dict:
    status = "🟢 运行中" if t["enabled"] else "⏸️ 已暂停"
    keywords = ", ".join(t.get("keywords") or []) or "（未配置）"

    return _card(
        "📄 任务详情", THEME["info"],
        [
            _div(f"**{t['name']}**  {status}"),
            _div(f"🔗 **URL**：{t['url']}"),
            _hr(),
            _fields([
                ("🆔 任务 ID", f"#{t['id']}", True),
                ("⏱️ 间隔", f"{t['interval']} 秒", True),
                ("🎯 类型", t.get("type", "html"), True),
                ("🤖 策略", t.get("strategy", "auto"), True),
                ("🎭 浏览器", t.get("impersonate", "chrome131"), True),
                ("📊 检查次数", str(t.get("total_checks", 0)), True),
                ("🔔 变更次数", str(t.get("total_changes", 0)), True),
                ("❌ 连续失败", str(t.get("consecutive_failures", 0)), True),
            ]),
            _div(f"🎯 **关键字过滤**：{keywords}"),
            _div(f"🕐 **上次检查**：{t.get('last_checked_at') or '从未'}"),
            _div(f"🔔 **上次变更**：{t.get('last_changed_at') or '从未'}"),
            _hr(),
            _action(_task_buttons(t["id"], t["enabled"], url=t["url"])),
        ],
        subtitle=f"#{t['id']} · {t['name']}",
    )


# ============================================================
# 7. 历史变更
# ============================================================
def history_card(task_name: str, task_id: int, items: list[dict]) -> dict:
    if not items:
        return _card(
            "📜 历史变更", THEME["info"],
            [_div(f"**{task_name}** #{task_id}"),
             _note("（暂无历史变更记录）")],
        )

    elements: list[dict] = [
        _div(f"📜 **{task_name}** #{task_id} 的最近变更："),
        _hr(),
    ]
    for it in items:
        matched = ", ".join(it.get("matched_keywords") or []) or "无"
        elements.append(_div(
            f"🕐 `{it['created_at']}` · "
            f"➕{it['added_lines']} / ➖{it['removed_lines']} · "
            f"📈{it['change_ratio']:.2%} · 🎯 {matched}"
        ))
    return _card("📜 历史变更", THEME["info"], elements, subtitle=task_name)


# ============================================================
# 8. 服务状态
# ============================================================
def status_card(data: dict) -> dict:
    return _card(
        "📊 服务状态", THEME["success"],
        [
            _div("💚 **服务健康状态**"),
            _hr(),
            _fields([
                ("⏱️ 运行时长", data.get("uptime", "-"), True),
                ("📋 任务", f"{data.get('total_tasks', 0)} "
                            f"({data.get('active_tasks', 0)} 活跃)", True),
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
        ],
    )


# ============================================================
# 9. 日志查看
# ============================================================
def log_card(tail_text: str, log_size: str = "", date: str = "") -> dict:
    display = tail_text
    if len(display) > 3800:
        display = "…\n" + display[-3800:]
    subtitle = f"{date} · {log_size}" if (date or log_size) else ""
    return _card(
        "📝 日志查看", THEME["info"],
        [
            _div("📝 **当日日志（末尾部分）**"),
            _div(f"```\n{display}\n```"),
            _hr(),
            _note("📎 完整日志已作为附件发送"),
        ],
        subtitle=subtitle,
    )


# ============================================================
# 10. 配置查看
# ============================================================
def config_card(cfg: dict) -> dict:
    return _card(
        "⚙️ 全局配置", THEME["info"],
        [
            _div("⚙️ **全局配置概览**"),
            _hr(),
            _fields([
                ("⏱️ 默认间隔", f"{cfg.get('default_check_interval', '-')}s", True),
                ("🚀 最大并发", str(cfg.get("max_concurrent_fetch", "-")), True),
                ("⏰ 域名限流", f"{cfg.get('domain_min_interval', '-')}s", True),
                ("⌛ 请求超时", f"{cfg.get('request_timeout', '-')}s", True),
                ("🌊 抖动", f"{cfg.get('jitter_ratio', 0):.1%}", True),
                ("📉 噪音阈值", f"{cfg.get('min_change_ratio', 0):.2%}", True),
                ("❄️ 推送冷却", f"{cfg.get('push_cooldown_seconds', '-')}s", True),
                ("🔔 失败告警", f"连续 {cfg.get('alert_after_consecutive_failures', '-')} 次", True),
            ]),
            _hr(),
            _div(
                f"**🎭 代理**：{cfg.get('proxy_info', '未启用')}\n"
                f"**☁️ 外部 API**：{cfg.get('external_apis', '未启用')}"
            ),
        ],
    )


# ============================================================
# 11. 错误 / 成功 / 失败告警
# ============================================================
def error_card(title: str, reason: str, suggestion: str = "") -> dict:
    content = f"❌ **{title}**\n\n{reason}"
    if suggestion:
        content += f"\n\n💡 建议：{suggestion}"
    return _card("⚠️ 操作失败", THEME["error"], [_div(content)])


def success_card(title: str, detail: str = "") -> dict:
    content = f"✅ **{title}**"
    if detail:
        content += f"\n\n{detail}"
    return _card("✨ 操作成功", THEME["success"], [_div(content)])


def fetch_failure_card(task_id: int, task_name: str, url: str,
                       consecutive_failures: int, error: str) -> dict:
    return _card(
        "🚨 抓取失败告警", THEME["warning"],
        [
            _div(f"🚨 **抓取连续失败告警**\n\n"
                 f"任务【{task_name}】已连续 **{consecutive_failures}** 次失败。"),
            _hr(),
            _fields([
                ("🆔 任务 ID", f"#{task_id}", True),
                ("❌ 连续失败", f"{consecutive_failures} 次", True),
            ]),
            _div(f"🔗 **URL**：{url}"),
            _div(f"```\n{error[:500]}\n```"),
            _hr(),
            _note("💡 试试 `/debug <ID>` 诊断，或 `/reset <ID> --strategy jina`"),
            _action([
                _btn("⚡ 立即重试", "check", task_id, style="primary"),
                _btn("⏸️ 暂停", "pause", task_id),
            ]),
        ],
        subtitle=task_name,
    )


# ============================================================
# 12. 抓包助手
# ============================================================
def sniff_helper_card(url: str) -> dict:
    return _card(
        "🔍 抓包助手", THEME["info"],
        [
            _div(
                f"🔍 **抓包助手**\n\n"
                f"对于 SPA 网站，**请求内部 API 比渲染页面更稳定、更快**。\n\n"
                f"目标：`{url}`"
            ),
            _hr(),
            _div(
                "**📖 操作步骤：**\n\n"
                "1️⃣ Chrome 打开上面这个网址\n"
                "2️⃣ `F12` → **Network** → 筛选 **Fetch/XHR**\n"
                "3️⃣ 刷新页面，观察列表\n"
                "4️⃣ 右键请求 → Copy → **Copy URL**"
            ),
            _hr(),
            _div(
                "**💡 识别 API 的特征：**\n"
                "• Content-Type: `application/json`\n"
                "• URL 含 `/api/` `/v1/` `/graphql`\n"
                "• Preview 能看到关心的数据字段"
            ),
            _note("`/add <api_url> --type json --json-path data[*].name` 创建 JSON 监控"),
        ],
    )


__all__ = [
    "startup_card", "help_card", "first_snapshot_card", "change_card",
    "task_list_card", "task_detail_card", "history_card",
    "status_card", "log_card", "config_card",
    "error_card", "success_card", "fetch_failure_card",
    "sniff_helper_card",
]
