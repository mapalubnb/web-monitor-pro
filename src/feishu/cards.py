"""
飞书卡片模板库（schema 2.0）

设计原则：
- 信息层级清晰：标题 → 关键指标 → 正文 → 提示
- 按钮仅在 /list 展示，其他卡片保持干净阅读体验
- 视觉统一：字段使用紧凑双列，分隔线划分段落
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
                                     "content": f"**{label}**\n{value}"}}
            for label, value, s in pairs
        ],
    }


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _task_buttons(task_id: int, enabled: bool, url: str | None = None,
                  has_snapshot: bool = False) -> list[dict]:
    """任务管理按钮行（仅用于 /list 列表卡片）。"""
    btns: list[dict] = []
    if url:
        btns.append(_btn("🔗 打开", "open_url", task_id, url=url))
    btns.append(_btn("⚡ 检查", "check", task_id))
    btns.append(_btn("📄 详情", "task_detail", task_id))
    if has_snapshot:
        btns.append(_btn("📥 快照", "snapshot", task_id))
    if enabled:
        btns.append(_btn("⏸ 暂停", "pause", task_id))
    else:
        btns.append(_btn("▶ 恢复", "resume", task_id, style="primary"))
    btns.append(_btn("🗑 删除", "remove", task_id, style="danger"))
    return btns


# ============================================================
# 1. 启动卡片
# ============================================================
def startup_card(task_count: int, default_interval: int,
                 version: str = "0.1.0") -> dict:
    return _card(
        "🚀 服务已启动", THEME["startup"],
        [
            _div("**Web Monitor Pro 网页监控服务已就绪**\n"
                 "调度器与风控策略全部加载完成。"),
            _hr(),
            _fields([
                ("监控任务", f"**{task_count}** 个", True),
                ("检查间隔", f"**{default_interval}** 秒", True),
                ("启动时间", f"`{_now()}`", True),
                ("版本", f"`v{version}`", True),
            ]),
            _hr(),
            _note("发送 `/help` 查看命令 · `/list` 查看任务列表"),
        ],
    )


# ============================================================
# 2. 命令帮助
# ============================================================
def help_card() -> dict:
    sections = [
        ("任务管理",
         "`/add <url>` — 新增监控，自动选择抓取方式\n"
         "`/add <url> --selector \"main\"` — 只监控页面指定区域\n"
         "`/add <api_url> --type json --json-path data.items` — 监控 API 字段\n"
         "`/list` — 列出所有任务\n"
         "`/pause <id>` · `/resume <id>` · `/remove <id>`\n"
         "`/check <id>` — 立即检查\n"
         "`/history <id>` — 变更历史\n"
         "`/snapshot <id>` — 下载快照\n"
         "`/interval <id> <秒>` — 修改间隔"),
        ("精细配置",
         "`/keyword <id> add <词1>[, 词2]` — 添加关键字\n"
         "`/keyword <id> remove / list / clear`\n"
         "`/sniff <url>` — 抓包助手\n"
         "`/debug <id>` — 自动诊断并给出修复建议\n"
         "`/reset <id> [选项]` — 重置基准或高级调参"),
        ("服务管理",
         "`/status` — 健康状态\n"
         "`/config` — 全局配置\n"
         "`/log [--tail N]` — 查看日志\n"
         "`/mute 30m` · `/unmute` — 免打扰"),
    ]
    elements: list[dict] = [
        _div("**命令手册**　　@机器人 或私聊发送命令均可"),
        _hr(),
    ]
    for title, content in sections:
        elements.append(_div(f"**{title}**\n{content}"))
        elements.append(_hr())
    elements.append(_note(
        "通常只需要 `/add <url>`；设置了 `--selector` 时会自动启用自适应重定位。"
    ))
    return _card("📘 命令帮助", THEME["info"], elements)


# ============================================================
# 3. 首次快照
# ============================================================
def first_snapshot_card(task_id: int, task_name: str, url: str,
                        content_length: int, strategy: str) -> dict:
    return _card(
        f"📸 已开始监控　#{task_id}", THEME["first"],
        [
            _div(f"**{task_name}** 已建立基准快照。"),
            _hr(),
            _fields([
                ("内容", f"**{content_length:,}** 字", True),
                ("策略", f"`{strategy}`", True),
            ]),
            _div(f"[🔗 {url}]({url})"),
            _note("后续只有确认变化时才会提醒。"),
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
                has_diff_file: bool = True,
                keyword_filtered: bool = False) -> dict:
    display = diff_summary or "（diff 摘要为空）"
    if len(display) > 1600:
        display = display[:1600] + "\n\n…（完整内容见附件）"

    elements: list[dict] = [
        _div(
            f"**{task_name}** 有新变化\n"
            f"`+{added_count}` / `-{removed_count}` · 变化 `{change_ratio:.2%}`"
        ),
    ]

    # 关键字命中提示
    if matched_keywords:
        kw_text = ", ".join(f"`{k}`" for k in matched_keywords)
        suffix = "，仅展示相关行" if keyword_filtered else ""
        elements.append(_div(f"🎯 命中：{kw_text}{suffix}"))

    elements.append(_hr())
    elements.append(_div(f"```diff\n{display}\n```"))

    note_parts = []
    if has_diff_file:
        note_parts.append("📎 完整 diff 见附件")
    note_parts.append(f"策略 `{strategy}`")
    note_parts.append(f"[🔗 打开页面]({url})")
    elements.append(_note(" · ".join(note_parts)))

    return _card(f"🔔 页面有变化　#{task_id}", THEME["change"], elements,
                 subtitle=task_name)


# ============================================================
# 5. 任务列表（唯一带管理按钮的卡片）
# ============================================================
def task_list_card(tasks: list[dict]) -> dict:
    if not tasks:
        return _card(
            "📋 任务列表", THEME["info"],
            [_div("暂无监控任务"),
             _note("发送 `/add <url>` 开始新建")],
        )

    elements: list[dict] = [
        _div(f"共 **{len(tasks)}** 个监控任务"),
        _hr(),
    ]
    for t in tasks:
        status = "🟢" if t["enabled"] else "⏸"
        last = t.get("last_checked_at") or "从未"
        keywords = t.get("keywords") or []
        kw_line = f"\n🎯 {', '.join(f'`{k}`' for k in keywords)}" if keywords else ""
        proxy = t.get("proxy_info") or "未启用"

        elements.append(_div(
            f"{status} **#{t['id']} · {t['name']}**\n"
            f"[打开页面]({t['url']}) · `{t['interval']}s` · "
            f"变更 `{t.get('total_changes', 0)}` · 最近 `{last}`"
            f"\n🌐 代理：{proxy}"
            f"{kw_line}"
        ))
        elements.append(_action(_task_buttons(
            t["id"], t["enabled"], url=t.get("url"),
            has_snapshot=t.get("has_snapshot", False),
        )))
        elements.append(_hr())

    elements.append(_note("添加任务通常只需 `/add <url>`"))
    return _card("📋 任务列表", THEME["info"], elements)


# ============================================================
# 6. 任务详情（无管理按钮，纯信息展示）
# ============================================================
def task_detail_card(t: dict) -> dict:
    status = "🟢 运行中" if t["enabled"] else "⏸ 已暂停"
    keywords = ", ".join(f"`{k}`" for k in (t.get("keywords") or [])) or "未配置"
    fails = t.get("consecutive_failures", 0)
    fail_text = f"**{fails}** 次" if fails else "无"

    # 实际使用的策略（优先显示）和配置策略
    actual = t.get("last_strategy_used") or ""
    configured = t.get("strategy", "auto")
    strategy_text = f"`{actual}`" if actual else f"`{configured}`（未执行）"

    advanced = []
    if t.get("adaptive_selector"):
        advanced.append(f"自适应 `{t.get('adaptive_threshold', 40)}`")
    if t.get("wait_selector"):
        advanced.append(f"等待 `{t['wait_selector']}`")
    advanced_text = " · ".join(advanced) or "自动"

    elements: list[dict] = [
        _div(f"**{t['name']}**　{status}"),
        _div(f"[🔗 {t['url']}]({t['url']})"),
        _hr(),
        _fields([
            ("类型", t.get("type", "html"), True),
            ("策略", strategy_text, True),
            ("检查间隔", f"{t['interval']} 秒", True),
            ("累计变更", str(t.get("total_changes", 0)), True),
            ("连续失败", fail_text, True),
            ("增强", advanced_text, True),
            ("代理", t.get("proxy_info") or "未启用", True),
        ]),
    ]

    # 提取配置（仅在配置了时显示）
    extract_parts: list[str] = []
    if t.get("selector"):
        extract_parts.append(f"CSS 选择器：`{t['selector']}`")
    if t.get("json_path"):
        extract_parts.append(f"JSON Path：`{t['json_path']}`")
    if t.get("extract_next_data"):
        extract_parts.append("SPA 数据提取：已启用")
    if extract_parts:
        elements.append(_hr())
        elements.append(_div("**提取配置**\n" + "\n".join(extract_parts)))

    elements.append(_hr())
    elements.append(_div(f"**关键字**：{keywords}"))
    elements.append(_fields([
        ("上次检查", t.get("last_checked_at") or "从未", True),
        ("上次变更", t.get("last_changed_at") or "从未", True),
    ]))
    elements.append(_hr())
    elements.append(
        _note("`/check` 立即检查 · `/history` 变更历史 · `/snapshot` 下载快照"))

    return _card(f"📄 任务详情　#{t['id']}", THEME["info"], elements,
                 subtitle=t["name"])


# ============================================================
# 7. 历史变更
# ============================================================
def history_card(task_name: str, task_id: int, items: list[dict]) -> dict:
    if not items:
        return _card(
            f"📜 变更历史　#{task_id}", THEME["info"],
            [_div(f"**{task_name}**"),
             _note("暂无历史变更记录")],
        )

    elements: list[dict] = [
        _div(f"**{task_name}** 最近 {len(items)} 条变更"),
        _hr(),
    ]
    for it in items:
        matched = ", ".join(it.get("matched_keywords") or [])
        kw_part = f"　🎯 {matched}" if matched else ""
        elements.append(_div(
            f"`{it['created_at']}`　"
            f"+{it['added_lines']} / -{it['removed_lines']}　"
            f"{it['change_ratio']:.2%}"
            f"{kw_part}"
        ))
    return _card(f"📜 变更历史　#{task_id}", THEME["info"], elements,
                 subtitle=task_name)


# ============================================================
# 8. 服务状态
# ============================================================
def status_card(data: dict) -> dict:
    return _card(
        "📊 服务状态", THEME["success"],
        [
            _fields([
                ("运行时长", data.get("uptime", "-"), True),
                ("任务", f"{data.get('active_tasks', 0)} 活跃 / "
                         f"{data.get('total_tasks', 0)} 总计", True),
                ("今日推送", str(data.get("pushes_today", 0)), True),
                ("今日检查", str(data.get("checks_today", 0)), True),
                ("今日错误", str(data.get("errors_today", 0)), True),
                ("免打扰", data.get("mute_status", "否"), True),
            ]),
            _hr(),
            _fields([
                ("主机", data.get("hostname", "-"), True),
                ("Python", data.get("python_version", "-"), True),
                ("版本", f"v{data.get('version', '-')}", True),
                ("内存", data.get("memory", "-"), True),
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
        "📝 日志", THEME["info"],
        [
            _div(f"```\n{display}\n```"),
            _hr(),
            _note("📎 完整日志见附件"),
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
            _fields([
                ("默认间隔", f"{cfg.get('default_check_interval', '-')}s", True),
                ("最大并发", str(cfg.get("max_concurrent_fetch", "-")), True),
                ("域名限流", f"{cfg.get('domain_min_interval', '-')}s", True),
                ("请求超时", f"{cfg.get('request_timeout', '-')}s", True),
                ("抖动比例", f"{cfg.get('jitter_ratio', 0):.1%}", True),
                ("噪音阈值", f"{cfg.get('min_change_ratio', 0):.2%}", True),
                ("推送冷却", f"{cfg.get('push_cooldown_seconds', '-')}s", True),
                ("失败告警", f"连续 {cfg.get('alert_after_consecutive_failures', '-')} 次", True),
            ]),
            _hr(),
            _div(
                f"**代理**：{cfg.get('proxy_info', '未启用')}\n"
                f"**增强模块**：{cfg.get('external_apis', '未启用')}"
            ),
        ],
    )


# ============================================================
# 11. 错误 / 成功 / 失败告警
# ============================================================
def error_card(title: str, reason: str, suggestion: str = "") -> dict:
    content = f"**{title}**\n\n{reason}"
    if suggestion:
        content += f"\n\n💡 {suggestion}"
    return _card("❌ 操作失败", THEME["error"], [_div(content)])


def success_card(title: str, detail: str = "") -> dict:
    content = f"**{title}**"
    if detail:
        content += f"\n\n{detail}"
    return _card("✅ 操作成功", THEME["success"], [_div(content)])


def fetch_failure_card(task_id: int, task_name: str, url: str,
                       consecutive_failures: int, error: str,
                       first_attempt: bool = False) -> dict:
    title = "🚨 首次抓取失败" if first_attempt else "🚨 抓取失败"
    summary = (
        f"任务 **{task_name}** 首次抓取未能建立基准快照"
        if first_attempt
        else f"任务 **{task_name}** 已连续 **{consecutive_failures}** 次失败"
    )
    hint = _failure_hint(error)
    elements = [
        _div(summary),
        _hr(),
        _div(f"[🔗 {url}]({url})"),
        _div(f"```\n{error[:500]}\n```"),
    ]
    if hint:
        elements.append(_div(f"**判断**\n{hint}"))
    elements.extend([
        _hr(),
        _note(f"`/debug {task_id}` 诊断 · "
              f"`/reset {task_id} --strategy scrapling_stealth` 切换策略"),
    ])
    return _card(
        f"{title}　#{task_id}", THEME["warning"],
        elements,
        subtitle=task_name,
    )


def _failure_hint(error: str) -> str:
    text = (error or "").lower()
    if (
        "free proxy failed" in text
        or "failed to connect" in text
        or "could not connect to server" in text
        or "curl: (7)" in text
        or "connectionerror" in text
    ):
        return (
            "疑似免费代理不可用。系统会冷却失败代理并直连重试；"
            "如仍连续失败，建议关闭免费代理池或配置稳定 `HTTPS_PROXY`。"
        )
    if "javascript verification" in text or "bot challenge" in text:
        return (
            "疑似机器人验证 / JS 挑战页，已拒绝作为有效快照。"
            "可尝试稳定代理、降低频率，或使用 `scrapling_stealth`。"
        )
    if "access denied" in text or "unauthorized" in text:
        return "疑似权限受限页面，已拒绝作为有效快照。请确认页面是否公开可访问。"
    if "content unusable" in text:
        return "页面返回内容不足或为空壳，已拒绝作为有效快照。可尝试 `/debug` 查看页面结构。"
    return ""


# ============================================================
# 12. 抓包助手
# ============================================================
def sniff_helper_card(url: str) -> dict:
    return _card(
        "🔍 抓包助手", THEME["info"],
        [
            _div(f"对于 SPA 网站，**请求内部 API 比渲染页面更快更稳定**。\n\n"
                 f"目标：`{url}`"),
            _hr(),
            _div(
                "**操作步骤**\n\n"
                "1. Chrome 打开目标网址\n"
                "2. `F12` → **Network** → 筛选 **Fetch/XHR**\n"
                "3. 刷新页面，观察请求列表\n"
                "4. 右键请求 → Copy → **Copy URL**"
            ),
            _hr(),
            _div(
                "**识别 API 的特征**\n"
                "- Content-Type: `application/json`\n"
                "- URL 含 `/api/`、`/v1/`、`/graphql`\n"
                "- Preview 能看到关心的数据字段"
            ),
            _hr(),
            _note("`/add <api_url> --type json --json-path data[*].name`"),
        ],
    )


__all__ = [
    "startup_card", "help_card", "first_snapshot_card", "change_card",
    "task_list_card", "task_detail_card", "history_card",
    "status_card", "log_card", "config_card",
    "error_card", "success_card", "fetch_failure_card",
    "sniff_helper_card",
]
