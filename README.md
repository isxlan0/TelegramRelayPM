# TelegramRelayPM

TelegramRelayPM 是一个使用 Go 重写的 Telegram 双向中继机器人。它可以把普通用户发给机器人的私聊消息转发给管理员，再把管理员的回复转回用户。

项目保留两种工作模式：

- `private`：用户私聊机器人，消息转发到管理员私聊；管理员通过回复转发消息，或用 `/session` 选定用户后回传。
- `group_topic`：用户私聊机器人，消息转发到管理员论坛超级群中的独立话题；管理员或群内成员在绑定用户的话题里发消息，会自动回传给该用户。

适合用户和管理员不直接私聊时，用机器人在双方之间转发消息。

## 当前 Go 版结构

```text
cmd/relaybot/main.go        程序入口、启动日志、长轮询启动、同名旧进程清理
internal/app/app.go         Update 处理、命令、消息转发、按钮回调
internal/config/config.go   .env 配置读取与校验
internal/domain/domain.go   话题标题、封禁时间、规则解析、统计时间窗口
internal/store/store.go     SQLite 表结构、查询、审计记录
internal/telegramx/client.go Telegram Bot API 封装
```

运行时会在当前目录生成 `YYYYMMDD_HHMMSS.log` 日志文件，并把日志同时输出到控制台。

## 功能

- 支持 `private` 和 `group_topic` 两种转发模式。
- 支持多个管理员，`ADMIN_CHAT_ID` 用 `|` 分隔。
- 用户消息会转发给管理员私聊，或转发到管理员群内的用户话题。
- `group_topic` 模式下会为首次私聊的用户自动创建话题。
- 用户话题标题格式为 `Full Name @username (user_id)`，没有用户名时为 `Full Name (user_id)`。
- 用户昵称或用户名变化后，会自动尝试更新话题标题。
- 管理员可用回复消息、`/session` 或话题绑定关系来指定回传目标。
- 绑定用户的话题中，普通群成员消息也可回传给对应用户。
- 可配置通用话题 `ADMIN_GROUP_GENERAL_THREAD_ID`，该话题不会被当成用户话题回传。
- 支持 `message`、`edited_message`、`channel_post`、`edited_channel_post` 等更新路径。
- 支持文本和媒体说明文字的编辑同步。
- 支持消息映射删除：`/deletepair`。
- 支持封禁、到期自动失效、原因和备注。
- 支持自动回复规则：精确、包含、前缀、正则。
- 支持 `/start` 内联按钮菜单，管理员和普通用户显示不同命令。
- 支持按钮引导输入，默认 180 秒超时。
- 支持广播，并可配置每个用户之间的发送间隔。
- 使用 SQLite 保存用户、消息映射、话题绑定、封禁、规则和审计记录。
- 启动时可同步机器人名称、简介、短简介和命令菜单到 Telegram。

## 运行要求

- Go 版本以 `go.mod` 为准，当前项目为 `go 1.26.1`。
- Telegram Bot Token，通过 `@BotFather` 创建机器人后获取。
- SQLite 不需要单独安装，项目使用纯 Go SQLite 驱动 `modernc.org/sqlite`。

## 安装与运行

### 1. 获取代码

```bash
git clone https://github.com/isxlan0/TelegramRelayPM.git
cd TelegramRelayPM
```

### 2. 创建配置文件

Linux / macOS：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`，至少填写：

```dotenv
BOT_TOKEN="123456:replace_with_your_bot_token"
ADMIN_CHAT_ID="123456789"
RELAY_MODE="private"
```

### 3. 下载依赖

```bash
go mod download
```

### 4. 直接运行

```bash
go run ./cmd/relaybot
```

程序启动后会读取 `.env`，初始化 SQLite 数据库，并开始 Telegram 长轮询。

## 编译

### 使用项目脚本

Windows 下可以直接运行：

```bat
build.bat
```

脚本会输出：

```text
dist/relaybot_linux_amd64
dist/relaybot_windows_amd64.exe
```

### 手动编译

当前平台：

```bash
go build -trimpath -ldflags "-s -w" -o relaybot ./cmd/relaybot
```

Linux amd64：

```bash
GOOS=linux GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o dist/relaybot_linux_amd64 ./cmd/relaybot
```

Windows amd64：

```bash
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags "-s -w" -o dist/relaybot_windows_amd64.exe ./cmd/relaybot
```

## 配置说明

示例配置见 `.env.example`。

| 配置项 | 必填 | 说明 |
| --- | --- | --- |
| `BOT_TOKEN` | 是 | Telegram Bot Token。 |
| `ADMIN_CHAT_ID` | 是 | 管理员用户 ID，不是群 ID。多个管理员用 `|` 分隔。 |
| `RELAY_MODE` | 否 | `private` 或 `group_topic`，默认 `private`。 |
| `ADMIN_GROUP_CHAT_ID` | `group_topic` 必填 | 管理员论坛超级群 ID，可填 `-100...`、`t.me/c/...` 链接或短 ID。 |
| `ADMIN_GROUP_GENERAL_THREAD_ID` | 否 | 通用话题 Thread ID。配置后，该话题内普通消息不会回传给用户。 |
| `DB_PATH` | 否 | SQLite 数据库路径，默认 `relay_bot.db`。 |
| `BROADCAST_DELAY_SECONDS` | 否 | 广播发送间隔，默认 `1.0` 秒。 |
| `START_MESSAGE` | 否 | `/start` 公告，换行请写 `\n`。 |
| `BOT_NAME` | 否 | 启动时同步到 Telegram 的机器人名称。 |
| `BOT_VERSION` | 否 | `/version` 显示的版本号。 |
| `BOT_DESCRIPTION` | 否 | 启动时同步到 Telegram 的机器人简介。 |
| `BOT_SHORT_DESCRIPTION` | 否 | 启动时同步到 Telegram 的机器人短简介。 |
| `BOT_USER_COMMANDS` | 否 | 普通用户命令菜单，格式为 `command:说明;command:说明`。 |
| `BOT_ADMIN_COMMANDS` | 否 | 管理员命令菜单，格式同上。 |
| `BOT_COMMANDS` | 否 | 旧配置名，仅在 `BOT_ADMIN_COMMANDS` 为空时读取。 |

注意：

- `.env` 中的值如果包含 `#`、空格或链接，建议使用双引号包裹。
- 多行文本请使用 `\n`。
- `ADMIN_GROUP_CHAT_ID="1234567890"` 会被程序转换为 `-1001234567890`。
- 可以在管理员群或话题内发送 `/chatid` 获取当前 Chat ID 和 Thread ID。

## `group_topic` 模式设置

1. 创建或选择一个超级群，并开启话题功能。
2. 把机器人加入群。
3. 建议把机器人设为管理员，并授予发送消息、创建话题、管理话题权限。
4. 如果要使用 `/deletepair` 删除双方消息，还需要给机器人删除消息权限。
5. 在 BotFather 中关闭机器人的隐私模式，否则机器人可能收不到群内普通消息。
6. 在群内发送 `/chatid`，把返回的群 ID 写入 `ADMIN_GROUP_CHAT_ID`。
7. 如需通用话题，在对应话题内发送 `/chatid`，把 Thread ID 写入 `ADMIN_GROUP_GENERAL_THREAD_ID`。
8. 设置 `RELAY_MODE="group_topic"` 后启动程序。

用户首次私聊机器人后，程序会在管理员群内创建该用户的话题。之后该用户发来的消息会进入同一话题，话题中的普通消息会回传给该用户。

## 命令

### 普通用户

- `/start`：查看公告和菜单。
- `/id`：查看自己的 Telegram 用户 ID。
- `/version`：查看机器人版本。

### 管理员

管理员命令可在管理员私聊中使用。`group_topic` 模式下，也可在配置的管理员群内使用。

- `/start`：查看管理员菜单。
- `/id`：查看自己的 Telegram 用户 ID。
- `/chatid`：查看当前 Chat ID 和 Thread ID。
- `/version`：查看机器人版本。
- `/recent [N]`：查看最近活跃用户，默认 10 条。
- `/session <user_id>`：在 `private` 模式下设置当前会话用户。
- `/session clear`：清空当前会话。
- `/ban <user_id> [1m|1h|1d|1w|YYYY-MM-DD] [原因|备注]`：封禁用户。
- `/ban`：回复用户转发消息时，封禁对应用户。
- `/banlist [N]`：查看有效封禁列表。
- `/baninfo <user_id>`：查看封禁详情。
- `/unban <user_id>`：解封用户。
- `/rule list`：查看自动回复规则。
- `/rule add <精确|包含|前缀|正则> <触发词> => <回复内容>`：添加规则。
- `/rule on <id>`：启用规则。
- `/rule off <id>`：停用规则。
- `/rule del <id>`：删除规则。
- `/rule test <文本>`：测试规则匹配。
- `/stats [24h|7d|30d]`：查看审计统计。
- `/sender`：查看发送者 ID，回复消息时查看被回复消息的发送者 ID。
- `/broadcast <文本>`：向所有已记录用户广播文本。
- `/broadcast`：回复一条消息时，广播被回复消息。
- `/deletepair`：回复映射消息时，删除双方对应消息。
- `/deletepair <管理员侧消息ID>`：按管理员侧消息 ID 删除映射消息。

封禁时间说明：

- `1m` 表示 1 分钟。
- `1h` 表示 1 小时。
- `1d` 表示 1 天。
- `1w` 表示 1 周。
- `YYYY-MM-DD` 表示 UTC 日期。
- 不写时间表示永久封禁。
- 原因和备注可用 `|` 分开，例如：`/ban 123456 1d spam | 多次广告`。

## 数据与日志

- 数据库默认文件为 `relay_bot.db`。
- 程序不会保存消息正文或媒体内容。
- SQLite 中保存用户 ID、用户名、昵称、消息 ID 映射、话题绑定、封禁、自动回复规则和审计记录。
- 每次启动都会生成一个新的 `.log` 文件，文件名格式为 `YYYYMMDD_HHMMSS.log`。
- 启动日志会记录机器人自身信息、管理员群信息和机器人在群内的权限状态。
- 旧 Python 版若已有 `relay_bot.db`，建议先备份再运行 Go 版；程序会创建缺少的表，并把旧 `banned_users` 中的数据写入新封禁表。

## 检查命令

```bash
go test ./...
```

没有测试文件时，该命令仍会检查所有包是否可编译。

## 常见问题

### 群组话题里发消息没有转给用户

先看最新 `.log` 文件：

- 如果完全没有“配置群组收到Update”，说明机器人没有收到群内消息，通常需要在 BotFather 关闭隐私模式，或调整群内权限。
- 如果日志显示话题未绑定用户，说明当前 Thread ID 没有对应用户记录；只有用户先私聊机器人后自动创建的话题，才会有绑定关系。
- 如果消息发在 `ADMIN_GROUP_GENERAL_THREAD_ID` 配置的话题内，程序会跳过回传。

### `ADMIN_GROUP_CHAT_ID` 应该填什么

可以填三种格式：

```dotenv
ADMIN_GROUP_CHAT_ID="1234567890"
ADMIN_GROUP_CHAT_ID="-1001234567890"
ADMIN_GROUP_CHAT_ID="https://t.me/c/1234567890/1"
```

推荐直接在群或话题内发送 `/chatid`，使用机器人返回的值。

### 为什么机器人名称或简介没有完全显示

Telegram 对名称、简介和短简介有长度限制。程序会在启动时按 Telegram 限制截断，并写入日志。

### 如何修改头像和用户名

头像和用户名不能通过 Bot API 设置，需要在 `@BotFather` 中修改。

## 许可证

本项目使用 MIT License，详见 `LICENSE`。

Author: `Xiao Lan`
