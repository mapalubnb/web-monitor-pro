# 飞书应用配置指南

完整走完本文档只需 **5–10 分钟**。你将获得一个可以私聊/群聊交互的飞书机器人，
服务器**无需开放公网端口**（采用 WebSocket 长连接）。

---

## 一、创建企业自建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/app) 并登录
2. 点击 **创建企业自建应用**
3. 填写应用名称（如 `Web 监控助手`）、图标、描述
4. 创建完成后进入应用详情页，记录 **App ID** 和 **App Secret**
   → 稍后填入 `.env` 的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`

---

## 二、开启机器人能力

左侧菜单 → **添加应用能力** → 选择 **机器人** → 开启

---

## 三、申请权限

左侧菜单 → **权限管理** → **开通下列权限**：

| 权限 | 作用 |
| --- | --- |
| `im:message` | 读取和发送单条消息 |
| `im:message.group_at_msg` | 接收群聊中被 @ 的消息 |
| `im:message.group_at_msg:readonly` | 同上（只读方便审批） |
| `im:message.p2p_msg` | 接收和发送单聊消息 |
| `im:message.p2p_msg:readonly` | 同上（只读方便审批） |
| `im:message:send_as_bot` | 以机器人身份发送消息 |
| `im:resource` | 上传/下载文件 |
| `im:chat:readonly` | 读取群信息 |

保存后，点击 **发布** → **版本创建并发布** → 等待管理员审批

---

## 四、开启长连接模式（**关键！**）

左侧菜单 → **事件与回调** → **事件配置** → 选择 **长连接模式**（Long Connection）

⚠️ **务必选择"长连接模式"**（而不是 HTTP 回调模式），这样你的服务器无需公网 IP 或域名。

---

## 五、订阅事件

左侧菜单 → **事件与回调** → **事件配置** → **添加事件**：

| 事件 | 说明 |
| --- | --- |
| `im.message.receive_v1` | 接收消息（用户发命令触发） |

保存。

---

## 六、配置卡片回调

左侧菜单 → **事件与回调** → **卡片回调** → 选择 **长连接模式**

这样用户点击卡片里的按钮时，回调事件会通过长连接推送到服务。

---

## 七、获取目标群的 `chat_id`

方法 1：**查群 chat_id（推荐，简单）**
1. 把机器人加到目标群
2. 在群里随便 @机器人 发一条消息
3. 跑起来服务后，观察日志，会看到 `chat_id=oc_xxxxxxxxxxxxxxxxxxxxx`
4. 把它填到 `.env` 的 `FEISHU_TARGET_CHAT_ID`

方法 2：通过 API 查询（高级）
```bash
curl -X GET "https://open.feishu.cn/open-apis/im/v1/chats" \
  -H "Authorization: Bearer <tenant_access_token>"
```

---

## 八、获取管理员 `open_id`（可选）

用于 `FEISHU_ADMIN_OPEN_IDS`，限制只有管理员能执行命令。

方法 1：发消息后从日志里看（最简单）  
给机器人发一条私聊消息，日志里会打印 `user=ou_xxxxxxxxxxxxxxxxxxxxx`

方法 2：用"批量获取用户 ID" API（见 [官方文档](https://open.feishu.cn/document/server-docs/contact-v3/user/batch_get_id)）

---

## 九、完整 `.env` 示例

```ini
FEISHU_APP_ID=cli_a1b2c3d4e5f6g7h8
FEISHU_APP_SECRET=abc1234567890abc1234567890abc12
FEISHU_TARGET_CHAT_ID=oc_1234567890abcdef1234567890abcdef
FEISHU_ADMIN_OPEN_IDS=ou_abcdef1234567890abcdef1234567890
DEFAULT_CHECK_INTERVAL=60
LOG_LEVEL=INFO
```

---

## 十、启动并验证

```bash
sudo systemctl restart web-monitor-pro
sudo journalctl -u web-monitor-pro -f
```

启动后如果看到日志：
```
🔌 正在建立飞书 WebSocket 长连接...
```
并且群里收到 `🚀 Web Monitor Pro 已启动` 的卡片——恭喜，配置完成！

---

## 常见问题

<details>
<summary><b>Q: 为什么用长连接而不是 webhook？</b></summary>

- 长连接不需要公网 IP 或域名，服务器更安全
- 不需要配置 HTTPS 证书
- 飞书会自动处理断线重连
- 在企业内网部署更方便
</details>

<details>
<summary><b>Q: 机器人发不出消息，日志报 99991672 / 权限不足</b></summary>

说明权限没通过审批，重新检查步骤三中的权限，并**点击发布等待审批通过**。
</details>

<details>
<summary><b>Q: 收不到用户消息？</b></summary>

- 检查"事件与回调"里是否订阅了 `im.message.receive_v1`
- 检查机器人是否已加入目标群
- 检查日志，看长连接是否正常建立
</details>
