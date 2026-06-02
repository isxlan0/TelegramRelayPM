package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"

	"github.com/joho/godotenv"
)

const (
	RelayModePrivate    = "private"
	RelayModeGroupTopic = "group_topic"
	defaultBotVersion   = "v1.0.3"
)

var validRelayModes = map[string]bool{
	RelayModePrivate:    true,
	RelayModeGroupTopic: true,
}

type Command struct {
	Command     string
	Description string
}

type Config struct {
	BotToken                  string
	AdminChatIDs              []int64
	AdminChatID               int64
	PrimaryAdminChatID        int64
	RelayMode                 string
	AdminGroupChatID          *int64
	AdminGroupGeneralThreadID *int
	DBPath                    string
	BroadcastDelaySeconds     float64
	StartMessage              string
	BotName                   string
	BotVersion                string
	BotDescription            string
	BotShortDescription       string
	BotUserCommands           []Command
	BotAdminCommands          []Command
}

func Load() (*Config, error) {
	_ = godotenv.Load()

	adminIDs, err := ParseAdminChatIDs(os.Getenv("ADMIN_CHAT_ID"))
	if err != nil {
		return nil, err
	}
	relayMode, err := ParseRelayMode(os.Getenv("RELAY_MODE"))
	if err != nil {
		return nil, err
	}
	adminGroupID, err := ParseAdminGroupChatID(os.Getenv("ADMIN_GROUP_CHAT_ID"))
	if err != nil {
		return nil, err
	}
	generalThreadID, err := OptionalIntEnv("ADMIN_GROUP_GENERAL_THREAD_ID")
	if err != nil {
		return nil, err
	}
	broadcastDelay, err := FloatEnv("BROADCAST_DELAY_SECONDS", "1.0")
	if err != nil {
		return nil, err
	}

	botVersion := strings.TrimSpace(os.Getenv("BOT_VERSION"))
	if botVersion == "" {
		botVersion = defaultBotVersion
	}

	cfg := &Config{
		BotToken:                  strings.TrimSpace(os.Getenv("BOT_TOKEN")),
		AdminChatIDs:              adminIDs,
		AdminChatID:               adminIDs[0],
		PrimaryAdminChatID:        adminIDs[0],
		RelayMode:                 relayMode,
		AdminGroupChatID:          adminGroupID,
		AdminGroupGeneralThreadID: generalThreadID,
		DBPath:                    strings.TrimSpace(envDefault("DB_PATH", "relay_bot.db")),
		BroadcastDelaySeconds:     broadcastDelay,
		StartMessage:              multilineEnv("START_MESSAGE"),
		BotName:                   strings.TrimSpace(os.Getenv("BOT_NAME")),
		BotVersion:                botVersion,
		BotDescription:            multilineEnv("BOT_DESCRIPTION"),
		BotShortDescription:       multilineEnv("BOT_SHORT_DESCRIPTION"),
		BotUserCommands:           ParseBotCommands(os.Getenv("BOT_USER_COMMANDS")),
	}
	adminCommandsRaw := os.Getenv("BOT_ADMIN_COMMANDS")
	if strings.TrimSpace(adminCommandsRaw) == "" {
		adminCommandsRaw = os.Getenv("BOT_COMMANDS")
	}
	cfg.BotAdminCommands = ParseBotCommands(adminCommandsRaw)

	if cfg.BotToken == "" {
		return nil, fmt.Errorf("环境变量 BOT_TOKEN 不能为空")
	}
	if cfg.RelayMode == RelayModeGroupTopic && cfg.AdminGroupChatID == nil {
		return nil, fmt.Errorf("RELAY_MODE=group_topic 时 ADMIN_GROUP_CHAT_ID 不能为空")
	}
	return cfg, nil
}

func envDefault(name, def string) string {
	value := os.Getenv(name)
	if strings.TrimSpace(value) == "" {
		return def
	}
	return value
}

func multilineEnv(name string) string {
	return strings.TrimSpace(strings.ReplaceAll(os.Getenv(name), `\n`, "\n"))
}

func ParseBotCommands(raw string) []Command {
	commands := make([]Command, 0)
	for _, item := range strings.Split(raw, ";") {
		item = strings.TrimSpace(item)
		if item == "" || !strings.Contains(item, ":") {
			continue
		}
		parts := strings.SplitN(item, ":", 2)
		command := strings.TrimLeft(strings.TrimSpace(parts[0]), "/")
		desc := strings.TrimSpace(parts[1])
		if command != "" && desc != "" {
			commands = append(commands, Command{Command: command, Description: desc})
		}
	}
	return commands
}

func IntEnv(name, def string) (int, error) {
	raw := strings.TrimSpace(envDefault(name, def))
	value, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("环境变量 %s 不是合法整数: %s", name, raw)
	}
	return value, nil
}

func OptionalIntEnv(name string) (*int, error) {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return nil, nil
	}
	value, err := strconv.Atoi(raw)
	if err != nil {
		return nil, fmt.Errorf("环境变量 %s 不是合法整数: %s", name, raw)
	}
	return &value, nil
}

func FloatEnv(name, def string) (float64, error) {
	raw := strings.TrimSpace(envDefault(name, def))
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return 0, fmt.Errorf("环境变量 %s 不是合法数字: %s", name, raw)
	}
	return value, nil
}

func ParseAdminChatIDs(raw string) ([]int64, error) {
	parts := strings.Split(raw, "|")
	adminIDs := make([]int64, 0, len(parts))
	seen := map[int64]bool{}
	for _, part := range parts {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		id, err := strconv.ParseInt(part, 10, 64)
		if err != nil || id <= 0 {
			return nil, fmt.Errorf("环境变量 ADMIN_CHAT_ID 包含非法ID: %s", part)
		}
		if !seen[id] {
			seen[id] = true
			adminIDs = append(adminIDs, id)
		}
	}
	if len(adminIDs) == 0 {
		return nil, fmt.Errorf("环境变量 ADMIN_CHAT_ID 不能为空")
	}
	return adminIDs, nil
}

func ParseRelayMode(raw string) (string, error) {
	value := strings.ToLower(strings.TrimSpace(raw))
	if value == "" {
		value = RelayModePrivate
	}
	if !validRelayModes[value] {
		return "", fmt.Errorf("环境变量 RELAY_MODE 仅支持 group_topic, private，当前值: %s", value)
	}
	return value, nil
}

func ParseAdminGroupChatID(raw string) (*int64, error) {
	text := strings.TrimSpace(raw)
	if text == "" {
		return nil, nil
	}
	candidate := text
	if idx := strings.Index(text, "t.me/c/"); idx >= 0 {
		candidate = text[idx+len("t.me/c/"):]
		candidate = strings.SplitN(candidate, "/", 2)[0]
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			return nil, fmt.Errorf("环境变量 ADMIN_GROUP_CHAT_ID 格式不正确: %s", text)
		}
	}
	if strings.HasPrefix(candidate, "-100") {
		value, err := strconv.ParseInt(candidate, 10, 64)
		if err != nil || value >= 0 {
			return nil, fmt.Errorf("环境变量 ADMIN_GROUP_CHAT_ID 不是合法群ID: %s", text)
		}
		return &value, nil
	}
	if isDigits(candidate) {
		shortID, _ := strconv.ParseInt(candidate, 10, 64)
		value := -(1_000_000_000_000 + shortID)
		return &value, nil
	}
	return nil, fmt.Errorf("环境变量 ADMIN_GROUP_CHAT_ID 格式不正确: %s。可填写 -100... 或 t.me/c/xxx/1 中的 xxx", text)
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, ch := range s {
		if ch < '0' || ch > '9' {
			return false
		}
	}
	return true
}

func (c *Config) IsAdmin(userID int64) bool {
	for _, id := range c.AdminChatIDs {
		if id == userID {
			return true
		}
	}
	return false
}

func (c *Config) IsAdminPrivateChat(chatID int64) bool {
	return c.IsAdmin(chatID)
}
