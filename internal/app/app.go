package app

import (
	"context"
	"fmt"
	"log"
	"strconv"
	"strings"
	"sync"
	"time"

	"telegramrelaypm/internal/config"
	"telegramrelaypm/internal/domain"
	"telegramrelaypm/internal/store"
	"telegramrelaypm/internal/telegramx"

	"github.com/mymmrac/telego"
)

type Store interface {
	TouchUser(ctx context.Context, userID int64, username, fullName string) error
	SaveMapping(ctx context.Context, m store.MessageMap) error
	GetTargetUserByAdminMessage(ctx context.Context, adminChatID int64, adminMessageID int) (*int64, error)
	GetUserToAdminMaps(ctx context.Context, userChatID int64, userMessageID int) ([]store.MessageMap, error)
	GetAdminToUserMaps(ctx context.Context, adminChatID int64, adminMessageID int) ([]store.MessageMap, error)
	GetMapsByAdminMessage(ctx context.Context, adminChatID int64, adminMessageID int) ([]store.MessageMap, error)
	GetRecentUsers(ctx context.Context, limit int, excludeUserID *int64) ([]store.User, error)
	GetAllUsers(ctx context.Context, excludeUserID *int64) ([]int64, error)
	SetCurrentSession(ctx context.Context, adminChatID int64, userID *int64) error
	GetCurrentSession(ctx context.Context, adminChatID int64) (*int64, error)
	GetUserTopic(ctx context.Context, userID int64) (*store.UserTopic, error)
	UpsertUserTopic(ctx context.Context, t store.UserTopic) error
	UpdateUserTopicTitle(ctx context.Context, userID int64, title string) error
	GetUserIDByTopic(ctx context.Context, adminGroupChatID int64, topicThreadID int) (*int64, error)
	BanUser(ctx context.Context, userID, operatorAdminID int64, reason, note, expiresAt *string) error
	UnbanUser(ctx context.Context, userID int64) (bool, error)
	GetBan(ctx context.Context, userID int64) (*store.Ban, error)
	IsUserBanned(ctx context.Context, userID int64) (bool, error)
	ListActiveBans(ctx context.Context, limit int) ([]store.Ban, error)
	AddAutoReplyRule(ctx context.Context, triggerType, triggerText, replyText string, priority int, createdByAdminID int64) (int64, error)
	ListAutoReplyRules(ctx context.Context, limit int) ([]store.Rule, error)
	SetAutoReplyRuleEnabled(ctx context.Context, ruleID int64, enabled bool) (bool, error)
	DeleteAutoReplyRule(ctx context.Context, ruleID int64) (bool, error)
	MatchAutoReplyRule(ctx context.Context, text string) (*store.Rule, error)
	RecordAuditEvent(ctx context.Context, e store.AuditEvent) error
	GetStatsCounts(ctx context.Context, sinceISO string) ([]store.StatCount, error)
	GetTopUsersByEvents(ctx context.Context, sinceISO string, limit int) ([]store.TopUser, error)
	DeleteMappingsByAdminMessage(ctx context.Context, adminChatID int64, adminMessageID int) (int64, error)
}

type PendingInput struct {
	Key          string
	OriginChatID int64
	CreatedAt    time.Time
}

type App struct {
	Cfg     *config.Config
	Store   Store
	Client  telegramx.Client
	pending map[int64]PendingInput
	mu      sync.Mutex
}

func New(cfg *config.Config, st Store, client telegramx.Client) *App {
	return &App{Cfg: cfg, Store: st, Client: client, pending: map[int64]PendingInput{}}
}

func (a *App) HandleUpdate(ctx context.Context, update telego.Update) {
	if a.Cfg.RelayMode == config.RelayModeGroupTopic && a.Cfg.AdminGroupChatID != nil {
		if msg := regularMessage(&update); msg != nil && msg.Chat.ID == *a.Cfg.AdminGroupChatID {
			log.Printf("配置群组收到Update：%s", updateLogMeta(update))
		}
	}
	if update.CallbackQuery != nil {
		a.handleCallback(ctx, update)
		return
	}
	if update.EditedMessage != nil || update.EditedChannelPost != nil {
		a.handleEditedMessage(ctx, update)
		return
	}
	if update.ChannelPost != nil {
		if telegramx.IsCommandLike(update.ChannelPost.Text, update.ChannelPost.Caption) {
			return
		}
		a.handleGroupTopicMessage(ctx, update)
		return
	}
	if update.Message == nil {
		return
	}
	msg := update.Message
	if command, args, ok := parseCommand(msg.Text); ok {
		a.handleCommand(ctx, update, command, args)
		return
	}
	if a.consumePending(ctx, update) {
		return
	}
	if msg.Chat.Type == telego.ChatTypePrivate {
		if msg.From != nil && a.Cfg.IsAdmin(msg.From.ID) {
			a.handleAdminMessage(ctx, update)
			return
		}
		a.handlePrivateUserMessage(ctx, update)
		return
	}
	if a.Cfg.RelayMode == config.RelayModeGroupTopic && a.Cfg.AdminGroupChatID != nil && msg.Chat.ID == *a.Cfg.AdminGroupChatID {
		a.handleGroupTopicMessage(ctx, update)
		return
	}
}

func (a *App) handleCommand(ctx context.Context, update telego.Update, command string, args []string) {
	msg := update.Message
	if msg == nil {
		return
	}
	switch command {
	case "start":
		a.startCmd(ctx, update)
	case "id":
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("你的 Telegram 用户 ID：%d", effectiveUserID(update)), nil)
	case "chatid":
		a.chatIDCmd(ctx, update)
	case "version":
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("当前机器人版本：%s", a.Cfg.BotVersion), nil)
	case "recent":
		a.recentCmd(ctx, update, args)
	case "session":
		a.sessionCmd(ctx, update, args)
	case "ban":
		a.banCmd(ctx, update, args)
	case "banlist":
		a.banListCmd(ctx, update, args)
	case "baninfo":
		a.banInfoCmd(ctx, update, args)
	case "unban":
		a.unbanCmd(ctx, update, args)
	case "rule":
		a.ruleCmd(ctx, update, args)
	case "stats":
		a.statsCmd(ctx, update, args)
	case "sender":
		a.senderCmd(ctx, update)
	case "broadcast":
		a.broadcastCmd(ctx, update, args)
	case "deletepair":
		a.deletePairCmd(ctx, update, args)
	default:
		if a.consumePending(ctx, update) {
			return
		}
	}
}

func (a *App) startCmd(ctx context.Context, update telego.Update) {
	msg := update.Message
	if msg == nil || msg.From == nil {
		return
	}
	if !a.Cfg.IsAdmin(msg.From.ID) {
		_ = a.Store.TouchUser(ctx, msg.From.ID, msg.From.Username, fullName(msg.From))
	}
	a.clearPending(msg.From.ID)
	commands := a.Cfg.BotUserCommands
	if a.Cfg.IsAdmin(msg.From.ID) {
		commands = a.Cfg.BotAdminCommands
	}
	text := a.Cfg.StartMessage
	if text == "" {
		text = "已连接中继机器人。发送 /id 查看你的 Telegram 用户 ID。"
	}
	var keyboard *telegramx.InlineKeyboard
	if msg.Chat.Type == telego.ChatTypePrivate || (a.isAdminCommandContext(update) && a.Cfg.RelayMode == config.RelayModeGroupTopic) {
		kb := startCommandsKeyboard(commands, a.Cfg.IsAdmin(msg.From.ID))
		keyboard = &kb
	}
	a.reply(ctx, msg.Chat.ID, threadPtr(msg), text, keyboard)
}

func (a *App) chatIDCmd(ctx context.Context, update telego.Update) {
	msg := update.Message
	if msg == nil {
		return
	}
	lines := []string{fmt.Sprintf("当前 Chat ID：%d", msg.Chat.ID)}
	if msg.MessageThreadID != 0 {
		lines = append(lines, fmt.Sprintf("当前话题 Thread ID：%d", msg.MessageThreadID))
	}
	if a.Cfg.AdminGroupChatID != nil {
		lines = append(lines, fmt.Sprintf("配置 ADMIN_GROUP_CHAT_ID：%d", *a.Cfg.AdminGroupChatID))
	}
	if a.Cfg.AdminGroupGeneralThreadID != nil {
		lines = append(lines, fmt.Sprintf("配置 ADMIN_GROUP_GENERAL_THREAD_ID：%d", *a.Cfg.AdminGroupGeneralThreadID))
	}
	a.reply(ctx, msg.Chat.ID, threadPtr(msg), strings.Join(lines, "\n"), nil)
}

func (a *App) recentCmd(ctx context.Context, update telego.Update, args []string) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	limit := parseLimit(args, 10, 1, 100)
	rows, err := a.Store.GetRecentUsers(ctx, limit, nil)
	if err != nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "查询失败。", nil)
		return
	}
	lines := []string{"最近活跃用户："}
	idx := 1
	for _, row := range rows {
		if a.Cfg.IsAdmin(row.UserID) {
			continue
		}
		uname := "-"
		if row.Username != "" {
			uname = "@" + row.Username
		}
		lines = append(lines, fmt.Sprintf("%d. %s | ID: %d | 用户名: %s | 最后活跃: %s", idx, row.FullName, row.UserID, uname, row.LastActiveAt))
		idx++
	}
	if len(lines) == 1 {
		lines = []string{"暂无用户记录。"}
	}
	a.reply(ctx, msg.Chat.ID, threadPtr(msg), strings.Join(lines, "\n"), nil)
}

func (a *App) sessionCmd(ctx context.Context, update telego.Update, args []string) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	if a.Cfg.RelayMode == config.RelayModeGroupTopic && a.Cfg.AdminGroupChatID != nil && msg.Chat.ID == *a.Cfg.AdminGroupChatID {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "群组话题模式下无需 /session，请直接在对应用户话题发送消息。", nil)
		return
	}
	adminChatID := a.adminChatID(update)
	if adminChatID == 0 {
		a.noPerm(ctx, msg)
		return
	}
	if len(args) == 0 {
		current, _ := a.Store.GetCurrentSession(ctx, adminChatID)
		if current == nil {
			a.reply(ctx, msg.Chat.ID, threadPtr(msg), "当前没有会话。用法：/session <用户ID> 或 /session clear", nil)
		} else {
			a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("当前会话用户 ID: %d", *current), nil)
		}
		return
	}
	if strings.EqualFold(args[0], "clear") {
		_ = a.Store.SetCurrentSession(ctx, adminChatID, nil)
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "已清空当前会话。", nil)
		return
	}
	uid, err := strconv.ParseInt(args[0], 10, 64)
	if err != nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "用法：/session <用户ID> 或 /session clear", nil)
		return
	}
	banned, _ := a.Store.IsUserBanned(ctx, uid)
	if banned {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("用户 %d 已封禁，不能设为当前会话。", uid), nil)
		return
	}
	_ = a.Store.SetCurrentSession(ctx, adminChatID, &uid)
	a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("当前会话已切换到用户：%d", uid), nil)
}

func (a *App) banCmd(ctx context.Context, update telego.Update, args []string) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	adminChatID := a.adminChatID(update)
	target, rest := a.targetFromArgOrReply(ctx, update, args)
	if target == nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "用法：/ban <用户ID> [1h|1d|7d|YYYY-MM-DD] [原因]，或回复用户转发消息后 /ban", nil)
		return
	}
	expires, reason, note := domain.ParseBanExtraArgs(rest)
	_ = a.Store.BanUser(ctx, *target, adminChatID, reason, note, expires)
	if cur, _ := a.Store.GetCurrentSession(ctx, adminChatID); cur != nil && *cur == *target {
		_ = a.Store.SetCurrentSession(ctx, adminChatID, nil)
	}
	ban, _ := a.Store.GetBan(ctx, *target)
	if ban != nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), store.FormatBanInfo(*ban), nil)
	} else {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("用户 %d 已封禁。", *target), nil)
	}
}

func (a *App) banListCmd(ctx context.Context, update telego.Update, args []string) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	rows, err := a.Store.ListActiveBans(ctx, parseLimit(args, 20, 1, 100))
	if err != nil || len(rows) == 0 {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "当前没有有效封禁。", nil)
		return
	}
	lines := []string{"当前封禁列表："}
	for i, row := range rows {
		reason := strings.TrimSpace(row.Reason)
		if reason == "" {
			reason = "-"
		}
		note := strings.TrimSpace(row.Note)
		if note == "" {
			note = "-"
		}
		lines = append(lines, fmt.Sprintf("%d. 用户ID: %d | 到期: %s | 原因: %s | 备注: %s", i+1, row.UserID, domain.FormatExpiryDisplay(row.ExpiresAt), reason, note))
	}
	a.reply(ctx, msg.Chat.ID, threadPtr(msg), strings.Join(lines, "\n"), nil)
}

func (a *App) banInfoCmd(ctx context.Context, update telego.Update, args []string) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	target, _ := a.targetFromArgOrReply(ctx, update, args)
	if target == nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "用法：/baninfo <用户ID>，或回复用户转发消息后 /baninfo", nil)
		return
	}
	ban, _ := a.Store.GetBan(ctx, *target)
	if ban == nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("用户 %d 当前不在有效封禁列表。", *target), nil)
		return
	}
	a.reply(ctx, msg.Chat.ID, threadPtr(msg), store.FormatBanInfo(*ban), nil)
}

func (a *App) unbanCmd(ctx context.Context, update telego.Update, args []string) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	target, _ := a.targetFromArgOrReply(ctx, update, args)
	if target == nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "用法：/unban <用户ID>，或回复用户转发消息后发送 /unban", nil)
		return
	}
	removed, _ := a.Store.UnbanUser(ctx, *target)
	if removed {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("用户 %d 已解封。", *target), nil)
	} else {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("用户 %d 当前不在封禁列表。", *target), nil)
	}
}

func (a *App) ruleCmd(ctx context.Context, update telego.Update, args []string) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	if len(args) == 0 || args[0] == "list" {
		rules, _ := a.Store.ListAutoReplyRules(ctx, 50)
		if len(rules) == 0 {
			a.reply(ctx, msg.Chat.ID, threadPtr(msg), "暂无自动回复规则。", nil)
			return
		}
		lines := []string{"自动回复规则："}
		for _, r := range rules {
			status := "停用"
			if r.IsEnabled {
				status = "启用"
			}
			lines = append(lines, fmt.Sprintf("#%d [%s] [%s] %s => %s", r.ID, status, r.TriggerType, r.TriggerText, r.ReplyText))
		}
		if len(strings.Join(lines, "\n")) > 3900 {
			lines = lines[:20]
		}
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), strings.Join(lines, "\n"), nil)
		return
	}
	switch args[0] {
	case "add":
		type_, trigger, reply, ok := domain.ParseRuleAddPayload(strings.Join(args[1:], " "))
		if !ok {
			a.reply(ctx, msg.Chat.ID, threadPtr(msg), "用法：/rule add <精确|包含|前缀|正则> <触发词> => <回复内容>", nil)
			return
		}
		id, _ := a.Store.AddAutoReplyRule(ctx, type_, trigger, reply, 100, a.adminChatID(update))
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("已添加规则 #%d。", id), nil)
	case "on", "off", "del":
		if len(args) < 2 {
			a.reply(ctx, msg.Chat.ID, threadPtr(msg), "用法：/rule on|off|del <规则ID>", nil)
			return
		}
		id, _ := strconv.ParseInt(args[1], 10, 64)
		var ok bool
		if args[0] == "del" {
			ok, _ = a.Store.DeleteAutoReplyRule(ctx, id)
		} else {
			ok, _ = a.Store.SetAutoReplyRuleEnabled(ctx, id, args[0] == "on")
		}
		if ok {
			a.reply(ctx, msg.Chat.ID, threadPtr(msg), "操作完成。", nil)
		} else {
			a.reply(ctx, msg.Chat.ID, threadPtr(msg), "规则不存在。", nil)
		}
	case "test":
		rule, _ := a.Store.MatchAutoReplyRule(ctx, strings.Join(args[1:], " "))
		if rule == nil {
			a.reply(ctx, msg.Chat.ID, threadPtr(msg), "未匹配规则。", nil)
		} else {
			a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("匹配规则 #%d，回复：%s", rule.ID, rule.ReplyText), nil)
		}
	default:
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "用法：/rule list|add|on|off|del|test", nil)
	}
}

func (a *App) statsCmd(ctx context.Context, update telego.Update, args []string) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	label, since := domain.ParseStatsWindow(firstArg(args, "24h"))
	counts, _ := a.Store.GetStatsCounts(ctx, since.Truncate(time.Second).Format(time.RFC3339))
	top, _ := a.Store.GetTopUsersByEvents(ctx, since.Truncate(time.Second).Format(time.RFC3339), 10)
	lines := []string{fmt.Sprintf("统计窗口：%s", label), "事件统计："}
	for _, c := range counts {
		lines = append(lines, fmt.Sprintf("- %s / %s: %d", c.EventType, c.Outcome, c.Count))
	}
	if len(counts) == 0 {
		lines = append(lines, "- 暂无")
	}
	lines = append(lines, "Top 用户：")
	for _, u := range top {
		lines = append(lines, fmt.Sprintf("- %d: %d", u.UserID, u.Count))
	}
	a.reply(ctx, msg.Chat.ID, threadPtr(msg), strings.Join(lines, "\n"), nil)
}

func (a *App) senderCmd(ctx context.Context, update telego.Update) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	if msg.ReplyToMessage != nil && msg.ReplyToMessage.From != nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("发送者ID：%d", msg.ReplyToMessage.From.ID), nil)
		return
	}
	a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("发送者ID：%d", effectiveUserID(update)), nil)
}

func (a *App) broadcastCmd(ctx context.Context, update telego.Update, args []string) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	if len(args) == 0 && msg.ReplyToMessage == nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "用法：/broadcast <文本>，或回复一条消息后 /broadcast", nil)
		return
	}
	users, err := a.Store.GetAllUsers(ctx, nil)
	if err != nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "读取用户列表失败。", nil)
		return
	}
	success, failed := 0, 0
	for _, uid := range users {
		if a.Cfg.IsAdmin(uid) {
			continue
		}
		var sentID int
		var err error
		if msg.ReplyToMessage != nil {
			sentID, err = a.Client.CopyMessage(ctx, telegramx.CopyMessageRequest{ChatID: uid, FromChatID: msg.Chat.ID, MessageID: msg.ReplyToMessage.MessageID})
			if err == nil {
				_ = a.Store.SaveMapping(ctx, store.MessageMap{UserChatID: uid, AdminChatID: msg.Chat.ID, UserMessageID: sentID, AdminMessageID: msg.ReplyToMessage.MessageID, Direction: "broadcast"})
			}
		} else {
			sentID, err = a.Client.SendMessage(ctx, telegramx.SendMessageRequest{ChatID: uid, Text: strings.Join(args, " ")})
			_ = sentID
		}
		outcome := "success"
		if err != nil {
			failed++
			outcome = "failed"
		} else {
			success++
		}
		_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: "broadcast_out", UserID: &uid, AdminChatID: &msg.Chat.ID, Outcome: outcome, Direction: strPtr("broadcast")})
		if a.Cfg.BroadcastDelaySeconds > 0 {
			time.Sleep(time.Duration(a.Cfg.BroadcastDelaySeconds * float64(time.Second)))
		}
	}
	a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("广播完成。成功：%d，失败：%d", success, failed), nil)
}

func (a *App) deletePairCmd(ctx context.Context, update telego.Update, args []string) {
	msg := update.Message
	if msg == nil || !a.isAdminCommandContext(update) {
		a.noPerm(ctx, msg)
		return
	}
	adminMsgID := 0
	if len(args) > 0 {
		adminMsgID, _ = strconv.Atoi(args[0])
	}
	if adminMsgID == 0 && msg.ReplyToMessage != nil {
		adminMsgID = msg.ReplyToMessage.MessageID
	}
	if adminMsgID == 0 {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "用法：/deletepair <管理员侧消息ID>，或回复映射消息后 /deletepair", nil)
		return
	}
	maps, _ := a.Store.GetMapsByAdminMessage(ctx, msg.Chat.ID, adminMsgID)
	for _, m := range maps {
		_ = a.Client.DeleteMessage(ctx, m.AdminChatID, m.AdminMessageID)
		_ = a.Client.DeleteMessage(ctx, m.UserChatID, m.UserMessageID)
	}
	deleted, _ := a.Store.DeleteMappingsByAdminMessage(ctx, msg.Chat.ID, adminMsgID)
	a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("已删除映射记录：%d", deleted), nil)
}

func (a *App) handlePrivateUserMessage(ctx context.Context, update telego.Update) {
	msg := update.Message
	if msg == nil || msg.From == nil {
		return
	}
	userID := msg.From.ID
	_ = a.Store.TouchUser(ctx, userID, msg.From.Username, fullName(msg.From))
	kind := messageKind(msg)
	_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: "user_msg_in", UserID: &userID, ChatID: &msg.Chat.ID, MessageID: &msg.MessageID, MessageKind: &kind, Outcome: "success"})
	banned, _ := a.Store.IsUserBanned(ctx, userID)
	if banned {
		_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: "blocked_ban", UserID: &userID, ChatID: &msg.Chat.ID, MessageID: &msg.MessageID, MessageKind: &kind, Outcome: "skipped"})
		return
	}
	if msg.Text != "" {
		if rule, _ := a.Store.MatchAutoReplyRule(ctx, msg.Text); rule != nil {
			a.reply(ctx, msg.Chat.ID, nil, rule.ReplyText, nil)
			_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: "auto_reply_hit", UserID: &userID, ChatID: &msg.Chat.ID, MessageID: &msg.MessageID, MessageKind: &kind, MappedMessageID: intPtr(int(rule.ID)), Outcome: "success", ErrorCode: strPtr(fmt.Sprintf("rule_id=%d", rule.ID))})
			return
		}
	}
	if a.Cfg.RelayMode == config.RelayModeGroupTopic {
		a.relayUserToTopic(ctx, msg)
		return
	}
	for _, adminID := range a.Cfg.AdminChatIDs {
		kb := adminActionKeyboard(userID, nil)
		sentID, err := a.Client.CopyMessage(ctx, telegramx.CopyMessageRequest{ChatID: adminID, FromChatID: msg.Chat.ID, MessageID: msg.MessageID, ReplyMarkup: &kb})
		outcome := "success"
		if err != nil {
			outcome = "failed"
			log.Printf("copy user message to admin failed: user=%d admin=%d err=%v", userID, adminID, err)
		} else {
			_ = a.Store.SaveMapping(ctx, store.MessageMap{UserChatID: userID, AdminChatID: adminID, UserMessageID: msg.MessageID, AdminMessageID: sentID, Direction: "user_to_admin"})
		}
		_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: "forward_user_to_admin", UserID: &userID, AdminChatID: &adminID, ChatID: &msg.Chat.ID, MessageID: &msg.MessageID, MappedMessageID: &sentID, MessageKind: &kind, Direction: strPtr("user_to_admin"), Outcome: outcome})
	}
}

func (a *App) relayUserToTopic(ctx context.Context, msg *telego.Message) {
	if a.Cfg.AdminGroupChatID == nil || msg.From == nil {
		return
	}
	userID := msg.From.ID
	threadID, err := a.ensureUserTopic(ctx, userID, msg.From.Username, fullName(msg.From))
	kind := messageKind(msg)
	if err != nil {
		log.Printf("ensure topic failed for user=%d err=%v", userID, err)
		_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: "forward_user_to_admin", UserID: &userID, AdminChatID: a.Cfg.AdminGroupChatID, ChatID: &msg.Chat.ID, MessageID: &msg.MessageID, MessageKind: &kind, Direction: strPtr("user_to_admin"), Outcome: "failed", ErrorClass: strPtr("topic")})
		return
	}
	kb := adminActionKeyboard(userID, nil)
	sentID, err := a.Client.CopyMessage(ctx, telegramx.CopyMessageRequest{ChatID: *a.Cfg.AdminGroupChatID, ThreadID: &threadID, FromChatID: msg.Chat.ID, MessageID: msg.MessageID, ReplyMarkup: &kb})
	outcome := "success"
	if err != nil {
		outcome = "failed"
		log.Printf("copy user message to topic failed: user=%d thread=%d err=%v", userID, threadID, err)
	} else {
		_ = a.Store.SaveMapping(ctx, store.MessageMap{UserChatID: userID, AdminChatID: *a.Cfg.AdminGroupChatID, UserMessageID: msg.MessageID, AdminMessageID: sentID, Direction: "user_to_admin"})
	}
	_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: "forward_user_to_admin", UserID: &userID, AdminChatID: a.Cfg.AdminGroupChatID, ChatID: &msg.Chat.ID, MessageID: &msg.MessageID, MappedMessageID: &sentID, MessageKind: &kind, Direction: strPtr("user_to_admin"), Outcome: outcome})
}

func (a *App) ensureUserTopic(ctx context.Context, userID int64, username, fullName string) (int, error) {
	if a.Cfg.AdminGroupChatID == nil {
		return 0, fmt.Errorf("ADMIN_GROUP_CHAT_ID is not configured")
	}
	expected := domain.BuildUserTopicTitle(username, fullName, userID)
	topic, err := a.Store.GetUserTopic(ctx, userID)
	if err != nil {
		return 0, err
	}
	if topic == nil {
		threadID, err := a.Client.CreateForumTopic(ctx, *a.Cfg.AdminGroupChatID, expected)
		if err != nil {
			return 0, err
		}
		return threadID, a.Store.UpsertUserTopic(ctx, store.UserTopic{UserID: userID, AdminGroupChatID: *a.Cfg.AdminGroupChatID, TopicThreadID: threadID, TopicTitle: expected})
	}
	if topic.TopicTitle != expected {
		if err := a.Client.EditForumTopic(ctx, *a.Cfg.AdminGroupChatID, topic.TopicThreadID, expected); err != nil {
			log.Printf("edit topic title failed for user %d: %v", userID, err)
		} else {
			_ = a.Store.UpdateUserTopicTitle(ctx, userID, expected)
		}
	}
	return topic.TopicThreadID, nil
}

func (a *App) handleAdminMessage(ctx context.Context, update telego.Update) {
	msg := regularMessage(&update)
	if msg == nil {
		return
	}
	target, err := a.resolveAdminTarget(ctx, update)
	if err != nil || target == nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "找不到目标用户。请回复用户消息，或使用 /session <用户ID>。", nil)
		return
	}
	banned, _ := a.Store.IsUserBanned(ctx, *target)
	if banned {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), fmt.Sprintf("用户 %d 已封禁，不能发送。", *target), nil)
		return
	}
	sentID, err := a.Client.CopyMessage(ctx, telegramx.CopyMessageRequest{ChatID: *target, FromChatID: msg.Chat.ID, MessageID: msg.MessageID})
	kind := messageKind(msg)
	outcome := "success"
	if err != nil {
		outcome = "failed"
		log.Printf("copy admin message to user failed: target=%d err=%v", *target, err)
	} else {
		_ = a.Store.SaveMapping(ctx, store.MessageMap{UserChatID: *target, AdminChatID: msg.Chat.ID, UserMessageID: sentID, AdminMessageID: msg.MessageID, Direction: "admin_to_user"})
	}
	_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: "forward_admin_to_user", UserID: target, AdminChatID: &msg.Chat.ID, ChatID: &msg.Chat.ID, MessageID: &msg.MessageID, MappedMessageID: &sentID, MessageKind: &kind, Direction: strPtr("admin_to_user"), Outcome: outcome})
}

func (a *App) handleGroupTopicMessage(ctx context.Context, update telego.Update) {
	msg := regularMessage(&update)
	if msg == nil || a.Cfg.RelayMode != config.RelayModeGroupTopic || a.Cfg.AdminGroupChatID == nil || msg.Chat.ID != *a.Cfg.AdminGroupChatID {
		return
	}
	if isServiceMessage(msg) || telegramx.IsCommandLike(msg.Text, msg.Caption) {
		return
	}
	target, err := a.resolveGroupTopicTarget(ctx, update)
	if err != nil || target == nil {
		kind := messageKind(msg)
		code := fmt.Sprintf("unbound_thread=%d", msg.MessageThreadID)
		_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: "forward_group_topic_to_user", ChatID: &msg.Chat.ID, MessageID: &msg.MessageID, MessageKind: &kind, Outcome: "skipped", ErrorCode: &code})
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "这个话题没有绑定用户，消息不会转发。", nil)
		return
	}
	banned, _ := a.Store.IsUserBanned(ctx, *target)
	if banned {
		return
	}
	sentID, err := a.Client.CopyMessage(ctx, telegramx.CopyMessageRequest{ChatID: *target, FromChatID: msg.Chat.ID, MessageID: msg.MessageID})
	kind := messageKind(msg)
	outcome := "success"
	if err != nil {
		outcome = "failed"
		log.Printf("copy group topic message failed: target=%d err=%v", *target, err)
	} else {
		_ = a.Store.SaveMapping(ctx, store.MessageMap{UserChatID: *target, AdminChatID: msg.Chat.ID, UserMessageID: sentID, AdminMessageID: msg.MessageID, Direction: "admin_to_user"})
	}
	_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: "forward_group_topic_to_user", UserID: target, AdminChatID: &msg.Chat.ID, ChatID: &msg.Chat.ID, MessageID: &msg.MessageID, MappedMessageID: &sentID, MessageKind: &kind, Direction: strPtr("admin_to_user"), Outcome: outcome})
}

func (a *App) resolveGroupTopicTarget(ctx context.Context, update telego.Update) (*int64, error) {
	msg := regularMessage(&update)
	if msg == nil || a.Cfg.AdminGroupChatID == nil {
		return nil, nil
	}
	if msg.ReplyToMessage != nil {
		if target, err := a.Store.GetTargetUserByAdminMessage(ctx, msg.Chat.ID, msg.ReplyToMessage.MessageID); err != nil || target != nil {
			return target, err
		}
	}
	if a.Cfg.AdminGroupGeneralThreadID != nil && msg.MessageThreadID == *a.Cfg.AdminGroupGeneralThreadID {
		return nil, nil
	}
	if msg.MessageThreadID == 0 {
		return nil, nil
	}
	return a.Store.GetUserIDByTopic(ctx, *a.Cfg.AdminGroupChatID, msg.MessageThreadID)
}

func (a *App) resolveAdminTarget(ctx context.Context, update telego.Update) (*int64, error) {
	msg := regularMessage(&update)
	if msg == nil {
		return nil, nil
	}
	if msg.ReplyToMessage != nil {
		if target, err := a.Store.GetTargetUserByAdminMessage(ctx, msg.Chat.ID, msg.ReplyToMessage.MessageID); err != nil || target != nil {
			return target, err
		}
	}
	if target, err := a.resolveGroupTopicTarget(ctx, update); err != nil || target != nil {
		return target, err
	}
	adminChatID := a.adminChatID(update)
	if adminChatID == 0 {
		return nil, nil
	}
	return a.Store.GetCurrentSession(ctx, adminChatID)
}

func (a *App) handleEditedMessage(ctx context.Context, update telego.Update) {
	msg := editedRegularMessage(&update)
	if msg == nil {
		return
	}
	if msg.Chat.Type == telego.ChatTypePrivate && msg.From != nil && !a.Cfg.IsAdmin(msg.From.ID) {
		maps, _ := a.Store.GetUserToAdminMaps(ctx, msg.Chat.ID, msg.MessageID)
		for _, m := range maps {
			a.syncEdit(ctx, msg, m.AdminChatID, m.AdminMessageID, "edit_sync_user_to_admin", &m.UserChatID)
		}
		return
	}
	if a.Cfg.RelayMode == config.RelayModeGroupTopic && a.Cfg.AdminGroupChatID != nil && msg.Chat.ID == *a.Cfg.AdminGroupChatID && telegramx.IsCommandLike(msg.Text, msg.Caption) {
		return
	}
	maps, _ := a.Store.GetAdminToUserMaps(ctx, msg.Chat.ID, msg.MessageID)
	for _, m := range maps {
		a.syncEdit(ctx, msg, m.UserChatID, m.UserMessageID, "edit_sync_admin_to_user", &m.UserChatID)
	}
}

func (a *App) syncEdit(ctx context.Context, msg *telego.Message, targetChatID int64, targetMessageID int, eventType string, userID *int64) {
	kind := messageKind(msg)
	outcome := "success"
	var err error
	if msg.Text != "" {
		err = a.Client.EditMessageText(ctx, telegramx.EditTextRequest{ChatID: targetChatID, MessageID: targetMessageID, Text: msg.Text, Entities: msg.Entities})
	} else if msg.Caption != "" {
		err = a.Client.EditMessageCaption(ctx, telegramx.EditCaptionRequest{ChatID: targetChatID, MessageID: targetMessageID, Caption: msg.Caption, Entities: msg.CaptionEntities})
	} else {
		outcome = "skipped"
	}
	if err != nil {
		outcome = "failed"
	}
	_ = a.Store.RecordAuditEvent(ctx, store.AuditEvent{EventType: eventType, UserID: userID, ChatID: &msg.Chat.ID, MessageID: &msg.MessageID, MappedMessageID: &targetMessageID, MessageKind: &kind, IsEdited: true, Outcome: outcome})
}

func (a *App) handleCallback(ctx context.Context, update telego.Update) {
	q := update.CallbackQuery
	if q == nil {
		return
	}
	data := q.Data
	_ = a.Client.AnswerCallback(ctx, q.ID, "", false)
	chatID := int64(0)
	var thread *int
	var msg *telego.Message
	if q.Message != nil {
		msg = q.Message.Message()
	}
	if msg != nil {
		chatID = msg.Chat.ID
		thread = threadPtr(msg)
	}
	if chatID == 0 {
		log.Printf("callback message unavailable: data=%s user=%d", data, q.From.ID)
		return
	}
	if strings.HasPrefix(data, "uid:") {
		a.callbackReply(ctx, chatID, thread, "用户ID："+strings.TrimPrefix(data, "uid:"))
		return
	}
	if data == "sessclear" {
		_ = a.Store.SetCurrentSession(ctx, chatID, nil)
		a.callbackReply(ctx, chatID, thread, "已清空当前会话。")
		return
	}
	if strings.HasPrefix(data, "sess:") {
		uid, _ := strconv.ParseInt(strings.TrimPrefix(data, "sess:"), 10, 64)
		_ = a.Store.SetCurrentSession(ctx, chatID, &uid)
		a.callbackReply(ctx, chatID, thread, fmt.Sprintf("当前会话已切换到用户：%d", uid))
		return
	}
	if strings.HasPrefix(data, "ban:") || strings.HasPrefix(data, "unban:") {
		parts := strings.SplitN(data, ":", 2)
		uid, _ := strconv.ParseInt(parts[1], 10, 64)
		if parts[0] == "ban" {
			_ = a.Store.BanUser(ctx, uid, q.From.ID, nil, nil, nil)
			a.callbackReply(ctx, chatID, thread, fmt.Sprintf("用户 %d 已封禁。", uid))
		} else {
			_, _ = a.Store.UnbanUser(ctx, uid)
			a.callbackReply(ctx, chatID, thread, fmt.Sprintf("用户 %d 已解封。", uid))
		}
		return
	}
	if strings.HasPrefix(data, "do:") {
		a.callbackReply(ctx, chatID, thread, "请使用对应 / 命令继续操作。")
		return
	}
	if strings.HasPrefix(data, "ask:") {
		a.setPending(q.From.ID, strings.TrimPrefix(data, "ask:"), chatID)
		a.callbackReply(ctx, chatID, thread, guidedPrompt(strings.TrimPrefix(data, "ask:")))
		return
	}
}

func (a *App) callbackReply(ctx context.Context, chatID int64, thread *int, text string) {
	if chatID != 0 {
		a.reply(ctx, chatID, thread, text, nil)
	}
}

func (a *App) consumePending(ctx context.Context, update telego.Update) bool {
	msg := update.Message
	if msg == nil || msg.From == nil || msg.Text == "" {
		return false
	}
	pending, ok := a.getPending(msg.From.ID)
	if !ok || pending.OriginChatID != msg.Chat.ID {
		return false
	}
	if time.Since(pending.CreatedAt) > domain.PendingInputTimeout {
		a.clearPending(msg.From.ID)
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "输入已超时，请重新点击菜单按钮。", nil)
		return true
	}
	a.clearPending(msg.From.ID)
	args := strings.Fields(msg.Text)
	switch pending.Key {
	case "recent", "session", "ban", "baninfo", "unban", "broadcast":
		a.handleCommand(ctx, update, pending.Key, args)
	case "rule:add":
		a.ruleCmd(ctx, update, append([]string{"add"}, strings.Split(msg.Text, " ")...))
	case "rule:test":
		a.ruleCmd(ctx, update, append([]string{"test"}, strings.Split(msg.Text, " ")...))
	default:
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "未知引导动作，已取消。", nil)
	}
	return true
}

func (a *App) reply(ctx context.Context, chatID int64, threadID *int, text string, keyboard *telegramx.InlineKeyboard) {
	if strings.TrimSpace(text) == "" {
		return
	}
	if len(text) > 4096 {
		text = text[:4096]
	}
	_, err := a.Client.SendMessage(ctx, telegramx.SendMessageRequest{ChatID: chatID, ThreadID: threadID, Text: text, ReplyMarkup: keyboard})
	if err != nil {
		log.Printf("send message failed: chat=%d err=%v", chatID, err)
	}
}

func (a *App) noPerm(ctx context.Context, msg *telego.Message) {
	if msg != nil {
		a.reply(ctx, msg.Chat.ID, threadPtr(msg), "无权限。", nil)
	}
}

func (a *App) isAdminCommandContext(update telego.Update) bool {
	msg := regularMessage(&update)
	if msg == nil || msg.From == nil || !a.Cfg.IsAdmin(msg.From.ID) {
		return false
	}
	if a.Cfg.IsAdminPrivateChat(msg.Chat.ID) {
		return true
	}
	return a.Cfg.RelayMode == config.RelayModeGroupTopic && a.Cfg.AdminGroupChatID != nil && msg.Chat.ID == *a.Cfg.AdminGroupChatID
}

func (a *App) adminChatID(update telego.Update) int64 {
	msg := regularMessage(&update)
	if msg == nil || msg.From == nil || !a.Cfg.IsAdmin(msg.From.ID) {
		return 0
	}
	if a.Cfg.IsAdminPrivateChat(msg.Chat.ID) {
		return msg.Chat.ID
	}
	if a.Cfg.RelayMode == config.RelayModeGroupTopic && a.Cfg.AdminGroupChatID != nil && msg.Chat.ID == *a.Cfg.AdminGroupChatID {
		return msg.Chat.ID
	}
	return 0
}

func (a *App) targetFromArgOrReply(ctx context.Context, update telego.Update, args []string) (*int64, []string) {
	if len(args) > 0 {
		if uid, err := strconv.ParseInt(args[0], 10, 64); err == nil {
			return &uid, args[1:]
		}
	}
	msg := update.Message
	if msg != nil && msg.ReplyToMessage != nil {
		if target, _ := a.Store.GetTargetUserByAdminMessage(ctx, msg.Chat.ID, msg.ReplyToMessage.MessageID); target != nil {
			return target, args
		}
	}
	return nil, args
}

func (a *App) setPending(userID int64, key string, originChatID int64) {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.pending[userID] = PendingInput{Key: key, OriginChatID: originChatID, CreatedAt: time.Now()}
}

func (a *App) getPending(userID int64) (PendingInput, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	p, ok := a.pending[userID]
	return p, ok
}

func (a *App) clearPending(userID int64) {
	a.mu.Lock()
	defer a.mu.Unlock()
	delete(a.pending, userID)
}

func parseCommand(text string) (string, []string, bool) {
	text = strings.TrimSpace(text)
	if !strings.HasPrefix(text, "/") {
		return "", nil, false
	}
	parts := strings.Fields(text)
	if len(parts) == 0 {
		return "", nil, false
	}
	cmd := strings.TrimPrefix(parts[0], "/")
	cmd = strings.SplitN(cmd, "@", 2)[0]
	return strings.ToLower(cmd), parts[1:], true
}

func regularMessage(update *telego.Update) *telego.Message {
	if update.Message != nil {
		return update.Message
	}
	return update.ChannelPost
}

func editedRegularMessage(update *telego.Update) *telego.Message {
	if update.EditedMessage != nil {
		return update.EditedMessage
	}
	return update.EditedChannelPost
}

func fullName(user *telego.User) string {
	if user == nil {
		return ""
	}
	name := strings.TrimSpace(strings.TrimSpace(user.FirstName) + " " + strings.TrimSpace(user.LastName))
	if name == "" {
		name = strconv.FormatInt(user.ID, 10)
	}
	return name
}

func effectiveUserID(update telego.Update) int64 {
	if update.Message != nil && update.Message.From != nil {
		return update.Message.From.ID
	}
	if update.CallbackQuery != nil {
		return update.CallbackQuery.From.ID
	}
	return 0
}

func threadPtr(msg *telego.Message) *int {
	if msg == nil || msg.MessageThreadID == 0 {
		return nil
	}
	return &msg.MessageThreadID
}

func messageKind(message *telego.Message) string {
	if message == nil {
		return "other"
	}
	if message.Text != "" {
		return "text"
	}
	if len(message.Photo) > 0 {
		return "photo"
	}
	if message.Video != nil {
		return "video"
	}
	if message.Document != nil {
		return "document"
	}
	if message.Audio != nil {
		return "audio"
	}
	if message.Voice != nil {
		return "voice"
	}
	if message.Sticker != nil {
		return "sticker"
	}
	if message.Animation != nil {
		return "animation"
	}
	if message.Location != nil {
		return "location"
	}
	if message.Contact != nil {
		return "contact"
	}
	return "other"
}

func isServiceMessage(message *telego.Message) bool {
	if message == nil {
		return false
	}
	return message.ForumTopicCreated != nil || message.ForumTopicEdited != nil || message.ForumTopicClosed != nil ||
		message.ForumTopicReopened != nil || message.GeneralForumTopicHidden != nil || message.GeneralForumTopicUnhidden != nil ||
		message.VideoChatScheduled != nil || message.VideoChatStarted != nil || message.VideoChatEnded != nil || message.VideoChatParticipantsInvited != nil ||
		len(message.NewChatMembers) > 0 || message.LeftChatMember != nil || message.NewChatTitle != "" || len(message.NewChatPhoto) > 0 ||
		message.DeleteChatPhoto || message.GroupChatCreated || message.SupergroupChatCreated || message.ChannelChatCreated ||
		message.MessageAutoDeleteTimerChanged != nil || message.MigrateToChatID != 0 || message.MigrateFromChatID != 0 || message.PinnedMessage != nil
}

func updateLogMeta(update telego.Update) string {
	msg := regularMessage(&update)
	if msg == nil {
		msg = editedRegularMessage(&update)
	}
	fields := []string{}
	if update.Message != nil {
		fields = append(fields, "message")
	}
	if update.ChannelPost != nil {
		fields = append(fields, "channel_post")
	}
	if update.EditedMessage != nil {
		fields = append(fields, "edited_message")
	}
	if update.EditedChannelPost != nil {
		fields = append(fields, "edited_channel_post")
	}
	if update.CallbackQuery != nil {
		fields = append(fields, "callback_query")
	}
	chatID, chatType, msgID, threadID, replyID := int64(0), "", 0, 0, 0
	if msg != nil {
		chatID = msg.Chat.ID
		chatType = msg.Chat.Type
		msgID = msg.MessageID
		threadID = msg.MessageThreadID
		if msg.ReplyToMessage != nil {
			replyID = msg.ReplyToMessage.MessageID
		}
	}
	return fmt.Sprintf("update=%d fields=%s chat=%d chat_type=%s user=%d message=%d thread=%d reply_to=%d", update.UpdateID, strings.Join(fields, ","), chatID, chatType, effectiveUserID(update), msgID, threadID, replyID)
}

func adminActionKeyboard(userID int64, adminMessageID *int) telegramx.InlineKeyboard {
	banData := fmt.Sprintf("ban:%d", userID)
	if adminMessageID != nil {
		banData = fmt.Sprintf("banmenu:%d:%d", userID, *adminMessageID)
	}
	rows := telegramx.InlineKeyboard{
		{{Text: "封禁用户", Data: banData}, {Text: "解封用户", Data: fmt.Sprintf("unban:%d", userID)}, {Text: "设为会话", Data: fmt.Sprintf("sess:%d", userID)}},
		{{Text: "清空会话", Data: "sessclear"}, {Text: "用户ID", Data: fmt.Sprintf("uid:%d", userID)}},
	}
	if adminMessageID != nil {
		rows = append(rows, []telegramx.Button{{Text: "删除消息", Data: fmt.Sprintf("delpair:%d", *adminMessageID)}})
	}
	return rows
}

func startCommandsKeyboard(commands []config.Command, isAdmin bool) telegramx.InlineKeyboard {
	buttons := []telegramx.Button{}
	seen := map[string]bool{}
	for _, c := range commands {
		cmd := strings.ToLower(strings.TrimSpace(c.Command))
		if cmd == "" || cmd == "start" {
			continue
		}
		data := "ask:" + cmd
		if cmd == "id" || cmd == "version" || cmd == "chatid" || cmd == "banlist" || cmd == "sender" || cmd == "deletepair" {
			data = "do:" + cmd
		} else if cmd == "stats" || cmd == "rule" {
			data = "menu:" + cmd
		}
		buttons = append(buttons, telegramx.Button{Text: fallback(c.Description, "/"+cmd), Data: data})
		seen[cmd] = true
	}
	if isAdmin {
		if !seen["stats"] {
			buttons = append(buttons, telegramx.Button{Text: "统计快捷", Data: "menu:stats"})
		}
		if !seen["rule"] {
			buttons = append(buttons, telegramx.Button{Text: "规则菜单", Data: "menu:rule"})
		}
	}
	if len(buttons) == 0 {
		return telegramx.InlineKeyboard{{{Text: "我的ID", Data: "do:id"}}}
	}
	rows := telegramx.InlineKeyboard{}
	for i := 0; i < len(buttons); i += 2 {
		end := i + 2
		if end > len(buttons) {
			end = len(buttons)
		}
		rows = append(rows, buttons[i:end])
	}
	return rows
}

func guidedPrompt(actionKey string) string {
	prompts := map[string]string{
		"recent":    "请输入数量 N（1-100），例如：10",
		"session":   "请输入用户ID，或输入 clear 清空当前会话。",
		"ban":       "请输入封禁参数：<用户ID> [1h|1d|7d|30d|YYYY-MM-DD] [原因]，备注用 | 分隔。",
		"baninfo":   "请输入要查询的用户ID。",
		"unban":     "请输入要解封的用户ID。",
		"broadcast": "请输入广播内容。",
		"rule:add":  "请输入：<精确|包含|前缀|正则> <触发词> => <回复内容>",
		"rule:test": "请输入要测试匹配的文本。",
	}
	return fallback(prompts[actionKey], "请输入参数。")
}

func parseLimit(args []string, def, min, max int) int {
	if len(args) == 0 {
		return def
	}
	value, err := strconv.Atoi(args[0])
	if err != nil {
		return def
	}
	if value < min {
		return min
	}
	if value > max {
		return max
	}
	return value
}

func firstArg(args []string, def string) string {
	if len(args) == 0 {
		return def
	}
	return args[0]
}

func fallback(value, def string) string {
	if strings.TrimSpace(value) == "" {
		return def
	}
	return value
}

func strPtr(value string) *string { return &value }
func intPtr(value int) *int       { return &value }
