# Telegram 双向中继机器人

一个基于 `python-telegram-bot` 的私聊中继机器人。  
适用于双方无法直接私聊（如被官方双向）时，通过机器人进行消息转发和回复。

## 功能特点

- 用户私聊机器人后，消息转发给管理员
- 管理员可直接回复转发消息，机器人自动回传给对应用户
- 支持管理员主动会话（`/session <user_id>`）
- 支持广播
- 支持文本/媒体编辑同步
- 支持映射消息对删除
- 通过 SQLite 数据库本地保存路由映射
- 普通用户与管理员命令菜单分离显示
- 机器人名称、简介、短简介、命令通过 `.env` 自动同步

## 环境要求

- Python
- Telegram Bot Token（通过 @BotFather 获取）

## 安装步骤

### 1. 拉取项目代码

```bash
git clone https://github.com/isxlan0/TelegramRelayPM.git
cd TelegramRelayPM
```

### 2. 从 BotFather 获取 Bot Token

1. 在 Telegram 搜索 `@BotFather`
2. 发送 `/newbot` 并按提示创建机器人
3. 获取 `BOT_TOKEN`
4. 发送 `/mybots` 可管理机器人资料（头像、用户名等）

### 3. 创建虚拟环境

```bash
python -m venv .venv
```

### 4. 激活虚拟环境

Linux / macOS:

```bash
source .venv/bin/activate
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### 5. 安装依赖

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 6. 创建 `.env` 文件

在项目根目录创建 `.env` 文件，示例内容如下：

```env
BOT_TOKEN="123456:replace_with_your_bot_token"
ADMIN_CHAT_ID="123456789"
DB_PATH="relay_bot.db"
BROADCAST_DELAY_SECONDS="0.1"
START_MESSAGE="公告：这里是双向消息中继机器人。\n如果你和对方无法直接私聊，可以直接在这里留言。"
BOT_NAME="双向中继机器人"
BOT_DESCRIPTION="这是一个双向消息中继机器人。\n当双方无法直接私聊时，可通过本机器人转发消息。"
BOT_SHORT_DESCRIPTION="双向消息中继"
BOT_USER_COMMANDS="start:查看公告与说明;id:查看你的用户ID"
BOT_ADMIN_COMMANDS="start:查看公告与说明;id:查看你的用户ID;recent:管理员查看最近活跃用户;session:管理员切换当前会话;ban:管理员封禁用户;unban:管理员解封用户;sender:管理员获取发送者ID;broadcast:管理员广播消息;deletepair:管理员删除映射消息对"
```

说明：

- `ADMIN_CHAT_ID` 是管理员的 Telegram 数字 ID
- 值里有 `#`、空格、链接时建议使用双引号
- 多行文本使用 `\n`

### 7. 不使用 Docker 运行

激活虚拟环境后运行：

```bash
python bot.py
```

## 使用说明

### 普通用户命令

- `/start` 查看公告
- `/id` 查看自己的 Telegram 用户 ID

### 管理员命令

- `/recent N` 查看最近活跃用户
- `/session <user_id>` 设置当前会话用户
- `/session clear` 清空当前会话
- `/session` 查看当前会话
- `/ban <user_id>` 或回复转发消息后 `/ban`，封禁用户（不再转发）
- `/unban <user_id>` 或回复转发消息后 `/unban`，解封用户
- `/sender` 回复一条转发消息，查询发送者 ID
- `/broadcast` 广播（回复消息优先；无回复时支持 `/broadcast 你好`）
- `/deletepair` 回复映射消息，删除双方对应消息

## 常见问题

### 1. 为什么短简介设置不完整？

- Telegram 对短简介长度有限制，超长会被截断
- 建议长文本放 `BOT_DESCRIPTION` 或 `START_MESSAGE`

### 2. 为什么 `.env` 内容被截断？

- `#` 在 `.env` 中可能被当注释
- 解决：整段内容使用双引号包裹

### 3. 为什么不能自动同步“删除消息”？

- 可用 `/deletepair` 手动删除映射消息对

### 4. 如何修改头像、用户名？

- 头像、用户名不能通过 Bot API 设置
- 需要在 `@BotFather` 中修改（`/setuserpic` 等）

## 数据与隐私

- 默认仅处理私聊消息
- 不保存消息正文/媒体内容
- 本地数据库仅保存转发映射和会话路由信息

## 许可证

本项目使用 MIT License，详见 `LICENSE`。  
Author: `Xiao Lan`
