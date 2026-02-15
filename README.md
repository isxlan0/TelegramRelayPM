# Telegram 双向中继机器人

一个基于 `python-telegram-bot` 的中继机器人，支持两种模式（通过 `.env` 二选一配置）：

- **私聊模式（private）**：用户私聊机器人后，消息转发给管理员私聊；管理员回复或用 `/session` 指定会话后回传给用户。
- **群组话题模式（group_topic）**：管理员在一个已开启话题（Forum）的超级群内管理用户；每个用户对应一个话题，管理员在该话题里直接发送即可自动转发给用户，无需 `/session`。

适用于双方无法直接私聊（如被官方双向）时，通过机器人进行消息转发和回复。

## 功能特点

- 支持两种转发模式（`.env` 配置 `RELAY_MODE=private|group_topic`）
- 用户私聊机器人后，消息可转发给管理员
- 群组话题模式下：
  - 每个用户首次私聊会自动在管理员群创建独立话题
  - 话题标题格式为 `Full Name @username (user_id)`（无 username 时为 `Full Name (user_id)`）
  - 用户再次私聊会复用原话题
  - 用户名/昵称变化时会自动更新话题标题
  - 管理员在对应话题里直接发送即可回传给用户（无需 `/session`）
- 管理员命令可在“管理员私聊 + 管理员群”执行（群组模式）
- 支持封禁增强（原因 / 备注 / 到期时间）
- 支持自动回复规则（命中后自动回复，不转发管理员）
- `/start` 支持内联按钮菜单：普通用户与管理员分离显示，管理员可在私聊与管理员群使用
- 按钮菜单支持两列紧凑排版，并可在菜单内跳转（主菜单/统计/规则）
- 规则管理支持按钮化操作：
  - “添加规则”后选择类型（精确/包含/前缀/正则）
  - 按钮引导输入 `触发词=>回复内容`（无需再输入类型前缀）
  - 启用/停用/删除支持规则列表按钮直接点击执行，列表显示当前启用状态
- 按钮引导输入支持取消与超时处理（默认 180 秒）
- 支持统计命令（`/stats`）
- 支持广播（可配置节流，建议 1 秒/用户）
- 支持文本/媒体编辑同步
- 支持映射消息对删除
- 通过 SQLite 数据库本地保存路由映射与审计元数据
- 普通用户与管理员命令菜单分离显示
- 机器人名称、简介、短简介、命令通过 `.env` 自动同步


## 测试机器人
- @yozora_sky_bot

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

示例内容：请查看.env.example


说明：

- `ADMIN_CHAT_ID` 是管理员用户的 Telegram 数字 ID（可多个，`|` 分隔）
- `RELAY_MODE` 为全局二选一：`private` 或 `group_topic`
- `group_topic` 模式下必须配置 `ADMIN_GROUP_CHAT_ID`，且该群必须开启话题（Forum）
- `ADMIN_GROUP_CHAT_ID` 推荐直接填写话题内的ID（随便点个话题进去https://t.me/c/xxx/1，这里的xxxx就是群ID） `1234567890`（程序自动转换为 `-1001234567890`）
- 管理员可在群内使用 `/chatid` 获取群 ID 与话题 ID（Thread ID）
- 广播建议 `BROADCAST_DELAY_SECONDS="1.0"`，约 1 秒 1 用户，降低限速风险
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
- `/session` 查看当前会话并显示最近活跃用户快捷按钮
- `/ban <user_id> [1h|1d|7d|YYYY-MM-DD] [reason]` 或回复转发消息后 `/ban`，封禁用户（支持到期自动解封）
- `/banlist [N]` 查看封禁列表
- `/baninfo <user_id>` 或回复转发消息后 `/baninfo`，查看封禁详情
- `/unban <user_id>` 或回复转发消息后 `/unban`，解封用户
- `/rule list` 查看自动回复规则
- `/rule add <精确|包含|前缀|正则> <触发词> => <回复内容>` 添加规则（也兼容 exact|contains|prefix|regex）
- `/rule on <id>` / `/rule off <id>` / `/rule del <id>` 管理规则（也可在菜单按钮中直接点选）
- `/rule test <文本>` 测试规则匹配
- `/stats [24h|7d|30d]` 查看审计统计
- `/sender` 回复一条转发消息，查询发送者 ID
- `/broadcast` 广播（回复消息优先；无回复时支持 `/broadcast 你好`）
- `/chatid` 管理员查看当前 chat_id / thread_id（群组话题模式配置用）
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

- 默认仅处理私聊消息（`RELAY_MODE=private`）
- 若开启群组话题模式（`RELAY_MODE=group_topic`），机器人仅处理：
  - 用户对机器人的私聊消息
  - 管理员论坛超级群（`ADMIN_GROUP_CHAT_ID`）内的消息
- 不保存消息正文/媒体内容
- 本地数据库仅保存转发映射、会话路由信息与审计元数据（不含消息正文）

## 许可证

本项目使用 MIT License，详见 `LICENSE`。  
Author: `Xiao Lan`
