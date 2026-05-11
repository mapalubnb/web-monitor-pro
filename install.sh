#!/usr/bin/env bash
# ============================================================
# Web Monitor Pro 一键安装脚本（Ubuntu 24.04 LTS）
# 功能：
#   1. 安装系统依赖（Python 3.11+、libcurl、编译工具）
#   2. 创建 Python 虚拟环境 + 安装依赖
#   3. 初始化配置文件（.env / config.yaml）
#   4. 注册 systemd 服务（开机自启、崩溃自恢复）
#   5. 打印下一步操作指引
# ============================================================

set -euo pipefail

# ---- 颜色输出 ----
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}ℹ️  $*${NC}"; }
ok()    { echo -e "${GREEN}✅ $*${NC}"; }
warn()  { echo -e "${YELLOW}⚠️  $*${NC}"; }
err()   { echo -e "${RED}❌ $*${NC}" >&2; }

# ---- 路径 ----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${SCRIPT_DIR}"
VENV_DIR="${PROJECT_DIR}/venv"
SERVICE_NAME="web-monitor-pro"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# ---- 参数 ----
SKIP_SYSTEMD=0
SKIP_APT=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-systemd) SKIP_SYSTEMD=1; shift ;;
        --no-apt)     SKIP_APT=1; shift ;;
        -h|--help)
            cat <<EOF
用法: sudo bash install.sh [选项]

选项:
  --no-apt        跳过 apt 包安装（已手动安装 Python/libcurl 时使用）
  --no-systemd    跳过 systemd 服务注册（仅配置 venv，不自动启动）
  -h, --help      显示本帮助

正常流程: sudo bash install.sh
EOF
            exit 0 ;;
        *) err "未知参数：$1"; exit 1 ;;
    esac
done

# ---- 权限检查 ----
if [[ ${SKIP_SYSTEMD} -eq 0 && ${EUID} -ne 0 ]]; then
    err "请用 sudo 运行本脚本（需要写 systemd 服务）"
    echo "   sudo bash install.sh"
    echo "   或使用 --no-systemd 参数跳过 systemd 步骤"
    exit 1
fi

# ---- 1. 系统依赖 ----
if [[ ${SKIP_APT} -eq 0 ]]; then
    info "安装系统依赖（apt）..."
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq \
        python3 python3-venv python3-pip python3-dev \
        build-essential libssl-dev libffi-dev \
        libxml2-dev libxslt1-dev \
        libcurl4-openssl-dev \
        ca-certificates curl git
    ok "系统依赖已就绪"
else
    info "已跳过 apt（--no-apt）"
fi

# ---- 2. Python 版本检查 ----
PY_CMD="$(command -v python3.12 || command -v python3.11 || command -v python3 || true)"
if [[ -z "${PY_CMD}" ]]; then
    err "未找到 Python，请先安装 python3.11+"
    exit 1
fi
PY_VERSION="$("${PY_CMD}" -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
info "使用 Python: ${PY_CMD} (版本 ${PY_VERSION})"

# 检查版本 >= 3.11
REQUIRED="3.11"
if [[ "$(printf '%s\n' "${REQUIRED}" "${PY_VERSION}" | sort -V | head -n1)" != "${REQUIRED}" ]]; then
    err "Python 版本过低（需要 >= 3.11，当前 ${PY_VERSION}）"
    exit 1
fi

# ---- 3. 虚拟环境 ----
if [[ ! -d "${VENV_DIR}" ]]; then
    info "创建 Python 虚拟环境：${VENV_DIR}"
    "${PY_CMD}" -m venv "${VENV_DIR}"
else
    info "虚拟环境已存在：${VENV_DIR}"
fi

info "升级 pip..."
"${VENV_DIR}/bin/pip" install --quiet --upgrade pip wheel setuptools

info "安装 Python 依赖（可能需要几分钟）..."
"${VENV_DIR}/bin/pip" install --quiet -r "${PROJECT_DIR}/requirements.txt"
ok "Python 依赖已安装"

info "安装 Playwright Chromium（约 120MB，纯 CSR 站点渲染用）..."
"${VENV_DIR}/bin/python" -m playwright install chromium 2>/dev/null || true
"${VENV_DIR}/bin/python" -m playwright install-deps chromium 2>/dev/null || true
ok "Playwright Chromium 已就绪"

# ---- 4. 初始化配置 ----
cd "${PROJECT_DIR}"

if [[ ! -f ".env" ]]; then
    cp .env.example .env
    warn "已创建 .env（当前是模板，请编辑填入飞书 AppID/Secret 等）"
    warn "  编辑命令：sudo vim ${PROJECT_DIR}/.env"
else
    info ".env 已存在，保留不动"
fi

if [[ ! -f "config.yaml" ]]; then
    cp config.example.yaml config.yaml
    info "已创建 config.yaml（默认示例任务均关闭，可按需启用）"
else
    info "config.yaml 已存在，保留不动"
fi

# 创建运行时目录
mkdir -p "${PROJECT_DIR}/data/logs" "${PROJECT_DIR}/data/snapshots"
chmod 755 "${PROJECT_DIR}/data"

# ---- 5. systemd 服务 ----
if [[ ${SKIP_SYSTEMD} -eq 0 ]]; then
    # 确定运行用户：如果是 root 下，建议创建专用用户；此处默认用当前 $SUDO_USER，退化为 root
    RUN_USER="${SUDO_USER:-root}"
    RUN_GROUP="$(id -gn "${RUN_USER}" 2>/dev/null || echo root)"

    info "配置 systemd 服务（用户：${RUN_USER}）..."

    cat > "${SERVICE_FILE}" <<EOF
[Unit]
Description=Web Monitor Pro - 网页变化监控 + 飞书交互机器人
Documentation=https://github.com/mapalubnb/web-monitor-pro
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${PROJECT_DIR}
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONPATH=${PROJECT_DIR}"
ExecStart=${VENV_DIR}/bin/python -m src.main
Restart=on-failure
RestartSec=10
StartLimitInterval=60
StartLimitBurst=5

# 稳定性与资源限制
LimitNOFILE=65535
StandardOutput=append:${PROJECT_DIR}/data/logs/systemd-stdout.log
StandardError=append:${PROJECT_DIR}/data/logs/systemd-stderr.log

# 安全加固
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=false
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    # 把 data 目录所有权还给运行用户
    chown -R "${RUN_USER}:${RUN_GROUP}" "${PROJECT_DIR}/data"

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}" >/dev/null
    ok "systemd 服务已注册：${SERVICE_FILE}"
else
    info "已跳过 systemd（--no-systemd）"
fi

# ---- 6. 完成提示 ----
cat <<EOF

${GREEN}══════════════════════════════════════════════════════════${NC}
${GREEN}    🎉  Web Monitor Pro 安装完成${NC}
${GREEN}══════════════════════════════════════════════════════════${NC}

📝 下一步操作：

  1️⃣  编辑 .env 填入飞书应用凭证
      ${YELLOW}vim ${PROJECT_DIR}/.env${NC}

      必填项：
        - FEISHU_APP_ID
        - FEISHU_APP_SECRET
        - FEISHU_TARGET_CHAT_ID

      👉 详细配置见 ${PROJECT_DIR}/docs/feishu-setup.md

  2️⃣  （可选）编辑 config.yaml 添加初始监控任务
      ${YELLOW}vim ${PROJECT_DIR}/config.yaml${NC}

  3️⃣  启动服务
      ${YELLOW}sudo systemctl start ${SERVICE_NAME}${NC}
      ${YELLOW}sudo systemctl status ${SERVICE_NAME}${NC}

  4️⃣  查看日志
      ${YELLOW}tail -f ${PROJECT_DIR}/data/logs/monitor_*.log${NC}
      ${YELLOW}sudo journalctl -u ${SERVICE_NAME} -f${NC}

  5️⃣  在飞书里发送 /help 开始使用

💡 常用命令:
    启动:    sudo systemctl start ${SERVICE_NAME}
    停止:    sudo systemctl stop ${SERVICE_NAME}
    重启:    sudo systemctl restart ${SERVICE_NAME}
    日志:    sudo journalctl -u ${SERVICE_NAME} -f

EOF
