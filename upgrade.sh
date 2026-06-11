#!/usr/bin/env bash
# ============================================================
# Web Monitor Pro 一键升级脚本
# 用法: cd ~/web-monitor-pro && sudo bash upgrade.sh
# ============================================================

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}ℹ️  $*${NC}"; }
ok()    { echo -e "${GREEN}✅ $*${NC}"; }
warn()  { echo -e "${YELLOW}⚠️  $*${NC}"; }
err()   { echo -e "${RED}❌ $*${NC}" >&2; }

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
SERVICE_NAME="web-monitor-pro"

# ---- 检查虚拟环境 ----
if [[ ! -d "${VENV_DIR}" ]]; then
    err "未找到虚拟环境 ${VENV_DIR}"
    err "看起来还没安装过，请先运行: sudo bash install.sh"
    exit 1
fi

PY="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

if [[ ! -f "${PY}" ]]; then
    err "虚拟环境中未找到 Python: ${PY}"
    exit 1
fi

echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}    Web Monitor Pro 升级脚本${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════════${NC}"
echo ""

# ---- 1. 停止服务 ----
info "停止服务..."
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    systemctl stop "${SERVICE_NAME}"
    ok "服务已停止"
else
    info "服务当前未运行，跳过"
fi

# ---- 2. 拉取最新代码 ----
info "拉取最新代码..."
cd "${PROJECT_DIR}"
git pull origin main
ok "代码已更新"

# ---- 3. 升级 Python 依赖 ----
info "升级 pip..."
"${PIP}" install --quiet --upgrade pip wheel setuptools

info "安装 Python 依赖..."
"${PIP}" install --quiet -r "${PROJECT_DIR}/requirements.txt"
ok "Python 依赖已更新"

# ---- 4. 安装 Playwright Chromium ----
info "安装 Playwright Chromium（约 120MB，首次安装需下载）..."
"${PY}" -m playwright install chromium 2>&1 || {
    warn "playwright install chromium 失败，尝试继续..."
}

info "安装 Playwright 系统依赖（需要 sudo）..."
"${PY}" -m playwright install-deps chromium 2>&1 || {
    warn "playwright install-deps 失败，Playwright 渲染功能可能不可用"
    warn "可手动运行: ${PY} -m playwright install-deps chromium"
}
ok "Playwright 已就绪"

# ---- 5. 更新 .env 配置 ----
info "更新 .env 配置..."
ENV_FILE="${PROJECT_DIR}/.env"

if [[ -f "${ENV_FILE}" ]]; then
    # 移除旧的 API key 配置
    REMOVED=0
    if grep -q 'JINA_READER_API_KEY' "${ENV_FILE}" 2>/dev/null; then
        sed -i '/JINA_READER_API_KEY/d' "${ENV_FILE}"
        REMOVED=1
    fi
    if grep -q 'JINA_READER_API_KEYS' "${ENV_FILE}" 2>/dev/null; then
        sed -i '/JINA_READER_API_KEYS/d' "${ENV_FILE}"
        REMOVED=1
    fi
    if grep -q 'FIRECRAWL_API_KEY' "${ENV_FILE}" 2>/dev/null; then
        sed -i '/FIRECRAWL_API_KEY/d' "${ENV_FILE}"
        REMOVED=1
    fi
    if grep -q 'ENABLE_GOOGLE_CACHE' "${ENV_FILE}" 2>/dev/null; then
        sed -i '/ENABLE_GOOGLE_CACHE/d' "${ENV_FILE}"
        REMOVED=1
    fi
    if [[ ${REMOVED} -eq 1 ]]; then
        ok "已移除旧配置项（Jina/Firecrawl/Google Cache）"
    fi

    # 添加新配置
    ADDED=0
    if ! grep -q 'ENABLE_PLAYWRIGHT' "${ENV_FILE}" 2>/dev/null; then
        echo 'ENABLE_PLAYWRIGHT=true' >> "${ENV_FILE}"
        ADDED=1
    fi
    if ! grep -q 'PLAYWRIGHT_TIMEOUT' "${ENV_FILE}" 2>/dev/null; then
        echo 'PLAYWRIGHT_TIMEOUT=30' >> "${ENV_FILE}"
        ADDED=1
    fi
    if ! grep -q 'PLAYWRIGHT_MAX_PAGES' "${ENV_FILE}" 2>/dev/null; then
        echo 'PLAYWRIGHT_MAX_PAGES=20' >> "${ENV_FILE}"
        ADDED=1
    fi
    if ! grep -q 'ENABLE_SCRAPLING' "${ENV_FILE}" 2>/dev/null; then
        echo 'ENABLE_SCRAPLING=true' >> "${ENV_FILE}"
        ADDED=1
    fi
    if ! grep -q 'ENABLE_FREE_PROXY_POOL' "${ENV_FILE}" 2>/dev/null; then
        {
            echo 'ENABLE_FREE_PROXY_POOL=false'
            echo 'FREE_PROXY_SOURCE_URL=https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/proxies/all/data.txt'
            echo 'FREE_PROXY_REFRESH_SECONDS=600'
            echo 'FREE_PROXY_MAX_COUNT=200'
        } >> "${ENV_FILE}"
        ADDED=1
    fi
    if [[ ${ADDED} -eq 1 ]]; then
        ok "已添加新增配置项"
    else
        info "新增配置项已存在，跳过"
    fi
else
    warn ".env 文件不存在，从模板创建..."
    cp "${PROJECT_DIR}/.env.example" "${ENV_FILE}"
    warn "请编辑 .env 填入飞书凭证: vim ${ENV_FILE}"
fi

# ---- 6. 确保数据目录存在 ----
mkdir -p "${PROJECT_DIR}/data/logs" "${PROJECT_DIR}/data/snapshots"

# ---- 7. 重新加载并启动服务 ----
info "重新加载 systemd 并启动服务..."
systemctl daemon-reload 2>/dev/null || true
systemctl start "${SERVICE_NAME}"

# 等待 2 秒检查状态
sleep 2
if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
    ok "服务已启动"
else
    err "服务启动失败，请检查日志:"
    echo "   sudo journalctl -u ${SERVICE_NAME} -n 30 --no-pager"
fi

# ---- 完成 ----
echo ""
echo -e "${GREEN}══════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}    🎉  升级完成！${NC}"
echo -e "${GREEN}══════════════════════════════════════════════════════════${NC}"
echo ""
echo -e "  查看状态:  ${YELLOW}sudo systemctl status ${SERVICE_NAME}${NC}"
echo -e "  查看日志:  ${YELLOW}sudo journalctl -u ${SERVICE_NAME} -f${NC}"
echo ""
echo -e "  本次升级内容:"
echo -e "    - 自建抓取链路: curl_cffi → httpx → deep extract → Scrapling → Playwright"
echo -e "    - Scrapling 自适应选择器与隐身抓取增强"
echo -e "    - 简化飞书卡片输出，默认自动启用选择器自适应"
echo -e "    - 可选 Proxifly 免费代理池（默认关闭）"
echo ""
