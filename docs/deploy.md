# 部署指南（Ubuntu 24.04 LTS）

## 快速部署（3 步）

```bash
# 1. 克隆代码
git clone https://github.com/mapalubnb/web-monitor-pro.git
cd web-monitor-pro

# 2. 一键安装（需要 sudo）
sudo bash install.sh

# 3. 配置飞书凭证
sudo vim .env      # 填入 FEISHU_APP_ID / APP_SECRET / TARGET_CHAT_ID
sudo systemctl start web-monitor-pro
sudo systemctl status web-monitor-pro
```

详细飞书配置见 [feishu-setup.md](feishu-setup.md)。

---

## 手动部署（不使用 install.sh）

```bash
# 系统依赖
sudo apt update
sudo apt install -y python3 python3-venv python3-pip python3-dev \
    build-essential libssl-dev libffi-dev libxml2-dev libxslt1-dev \
    libcurl4-openssl-dev

# 虚拟环境
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt

# 配置
cp .env.example .env
cp config.example.yaml config.yaml
vim .env                    # 填入飞书凭证

# 前台运行（调试用）
python -m src.main

# 守护运行（参考 install.sh 生成 systemd service）
```

---

## 常用命令

```bash
# 启动 / 停止 / 重启
sudo systemctl start web-monitor-pro
sudo systemctl stop web-monitor-pro
sudo systemctl restart web-monitor-pro

# 查看状态
sudo systemctl status web-monitor-pro

# 实时查看日志
sudo journalctl -u web-monitor-pro -f
tail -f data/logs/monitor_$(date +%F).log

# 查看中文日志（项目自身的）
tail -100 data/logs/monitor_$(date +%F).log

# 查看最近错误
tail -50 data/logs/error_$(date +%F).log

# 开机自启（install.sh 已自动启用）
sudo systemctl enable web-monitor-pro
sudo systemctl disable web-monitor-pro
```

---

## 升级

```bash
cd web-monitor-pro
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart web-monitor-pro
```

---

## 目录结构

```
web-monitor-pro/
├── src/                    # 源代码
├── venv/                   # Python 虚拟环境（install.sh 生成）
├── data/
│   ├── monitor.db          # SQLite 数据库
│   ├── logs/               # 日志（按天轮转，保留 30 天）
│   │   ├── monitor_YYYY-MM-DD.log
│   │   ├── error_YYYY-MM-DD.log
│   │   ├── systemd-stdout.log
│   │   └── systemd-stderr.log
│   └── snapshots/          # 快照 / diff 文件
│       ├── task_1_latest.txt
│       ├── task_1_20260510_143022.txt
│       └── task_1_diff_20260510_143022.diff
├── .env                    # 敏感配置（install.sh 生成，需手动填）
├── config.yaml             # 业务配置（install.sh 生成）
├── install.sh
├── uninstall.sh
└── requirements.txt
```

---

## 卸载

```bash
sudo bash uninstall.sh       # 仅卸载 systemd 服务，保留代码和数据
# 如需彻底清理：
rm -rf /path/to/web-monitor-pro
```

---

## 生产环境建议

1. **单独创建运行用户**（install.sh 默认用 `$SUDO_USER`）
   ```bash
   sudo useradd -m -s /bin/bash webmonitor
   sudo chown -R webmonitor:webmonitor /path/to/web-monitor-pro
   ```
   然后修改 `/etc/systemd/system/web-monitor-pro.service` 中的 `User=webmonitor`

2. **定期备份 SQLite**
   ```bash
   sqlite3 data/monitor.db ".backup data/monitor.db.bak"
   ```

3. **监控服务本身**（可选）
   ```bash
   # /etc/cron.hourly/check-web-monitor
   systemctl is-active --quiet web-monitor-pro || systemctl restart web-monitor-pro
   ```

4. **调整内存/CPU 限制**（systemd 的 [Service] 区）
   ```ini
   MemoryMax=512M
   CPUQuota=30%
   ```
