#!/usr/bin/env bash
# ============================================================
# Web Monitor Pro 卸载脚本
# 默认：仅卸载 systemd 服务，保留代码和数据
# --purge：同时删除 venv、data（数据库/快照/日志）、.env、config.yaml
# ============================================================

set -euo pipefail

SERVICE_NAME="web-monitor-pro"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PURGE=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge) PURGE=1; shift ;;
        -y|--yes) ASSUME_YES=1; shift ;;
        -h|--help)
            cat <<EOF
用法: sudo bash uninstall.sh [选项]

选项:
  --purge     同时删除 venv/data/.env/config.yaml（彻底清理）
  -y, --yes   非交互模式，--purge 时跳过二次确认（危险，自动化脚本使用）
  -h, --help  显示帮助

默认:           仅卸载 systemd 服务，代码和数据保留
sudo bash uninstall.sh --purge   彻底清理（除源码外，会要求二次确认）
EOF
            exit 0 ;;
        *) echo "未知参数：$1" >&2; exit 1 ;;
    esac
done

if [[ ${EUID} -ne 0 ]]; then
    echo "请用 sudo 运行" >&2
    exit 1
fi

echo "⏹️  停止服务..."
systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
echo "🚫 禁用开机自启..."
systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
echo "🗑️  移除 systemd 文件..."
rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload

if [[ ${PURGE} -eq 1 ]]; then
    echo ""
    echo "⚠️  --purge 模式将永久删除以下内容："
    echo "     ${SCRIPT_DIR}/venv"
    echo "     ${SCRIPT_DIR}/data      （数据库、快照、日志，无法恢复）"
    echo "     ${SCRIPT_DIR}/.env      （飞书凭证）"
    echo "     ${SCRIPT_DIR}/config.yaml"
    echo ""

    if [[ ${ASSUME_YES} -ne 1 ]]; then
        # 直接从 /dev/tty 读取，避免通过管道/自动化调用时被静默跳过
        if [[ ! -t 0 && ! -r /dev/tty ]]; then
            echo "❌ 非交互式终端且未指定 --yes，已中止。" >&2
            echo "   如确需在脚本里强制彻底清理，请加 --yes 参数（危险）" >&2
            exit 2
        fi
        read -r -p "请输入 'yes' 确认执行彻底清理： " CONFIRM </dev/tty
        if [[ "${CONFIRM}" != "yes" ]]; then
            echo "❌ 未输入 'yes'，已取消清理。systemd 已卸载，代码和数据保留。"
            exit 0
        fi
    fi

    echo "🧹 彻底清理 venv / data / .env / config.yaml ..."
    rm -rf "${SCRIPT_DIR}/venv"
    rm -rf "${SCRIPT_DIR}/data"
    rm -f  "${SCRIPT_DIR}/.env"
    rm -f  "${SCRIPT_DIR}/config.yaml"
    echo "✅ 已彻底清理（仅保留源码）"
else
    echo "✅ 已卸载 systemd 服务（代码和数据保留在项目目录）"
    echo "💡 如需彻底清理：sudo bash uninstall.sh --purge"
fi
