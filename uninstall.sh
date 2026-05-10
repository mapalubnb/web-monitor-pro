#!/usr/bin/env bash
# 卸载 Web Monitor Pro 的 systemd 服务（保留代码和数据）
set -euo pipefail

SERVICE_NAME="web-monitor-pro"

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

echo "✅ 已卸载 systemd 服务（代码和数据保留在项目目录）"
echo "💡 如需彻底清理，手动 rm -rf 项目目录即可"
