package telegramx

import (
	"context"
	"fmt"
	"log"
	"strings"

	"telegramrelaypm/internal/config"

	"github.com/mymmrac/telego"
)

const (
	MaxBotNameLen             = 64
	MaxBotDescriptionLen      = 512
	MaxBotShortDescriptionLen = 120
)

type Button struct {
	Text string
	Data string
}

type InlineKeyboard [][]Button

type ReplyRef struct {
	ChatID    int64
	MessageID int
}

type SendMessageRequest struct {
	ChatID      int64
	ThreadID    *int
	Text        string
	ReplyTo     *ReplyRef
	ReplyMarkup *InlineKeyboard
}

type CopyMessageRequest struct {
	ChatID      int64
	ThreadID    *int
	FromChatID  int64
	MessageID   int
	ReplyTo     *ReplyRef
	ReplyMarkup *InlineKeyboard
}

type EditTextRequest struct {
	ChatID    int64
	MessageID int
	Text      string
	Entities  []telego.MessageEntity
}

type EditCaptionRequest struct {
	ChatID    int64
	MessageID int
	Caption   string
	Entities  []telego.MessageEntity
}

type Client interface {
	SendMessage(ctx context.Context, req SendMessageRequest) (int, error)
	CopyMessage(ctx context.Context, req CopyMessageRequest) (int, error)
	EditMessageText(ctx context.Context, req EditTextRequest) error
	EditMessageCaption(ctx context.Context, req EditCaptionRequest) error
	DeleteMessage(ctx context.Context, chatID int64, messageID int) error
	CreateForumTopic(ctx context.Context, chatID int64, name string) (int, error)
	EditForumTopic(ctx context.Context, chatID int64, threadID int, name string) error
	AnswerCallback(ctx context.Context, queryID, text string, alert bool) error
	SetupProfile(ctx context.Context, cfg *config.Config) error
	GetMe(ctx context.Context) (*telego.User, error)
	GetChat(ctx context.Context, chatID int64) (*telego.ChatFullInfo, error)
	GetChatMember(ctx context.Context, chatID, userID int64) (telego.ChatMember, error)
}

type TelegoClient struct {
	Bot *telego.Bot
}

func New(token string) (*TelegoClient, error) {
	bot, err := telego.NewBot(token)
	if err != nil {
		return nil, err
	}
	return &TelegoClient{Bot: bot}, nil
}

func AllowedUpdates() []string {
	return []string{
		telego.MessageUpdates,
		telego.EditedMessageUpdates,
		telego.CallbackQueryUpdates,
		telego.ChannelPostUpdates,
		telego.EditedChannelPostUpdates,
		telego.MyChatMemberUpdates,
		telego.ChatMemberUpdates,
	}
}

func (c *TelegoClient) Updates(ctx context.Context) (<-chan telego.Update, error) {
	return c.Bot.UpdatesViaLongPolling(ctx, &telego.GetUpdatesParams{
		Timeout:        60,
		AllowedUpdates: AllowedUpdates(),
	})
}

func (c *TelegoClient) SendMessage(ctx context.Context, req SendMessageRequest) (int, error) {
	params := &telego.SendMessageParams{
		ChatID:      chatID(req.ChatID),
		Text:        req.Text,
		ReplyMarkup: toReplyMarkup(req.ReplyMarkup),
	}
	if req.ThreadID != nil {
		params.MessageThreadID = *req.ThreadID
	}
	if req.ReplyTo != nil {
		params.ReplyParameters = toReplyParameters(req.ReplyTo)
	}
	msg, err := c.Bot.SendMessage(ctx, params)
	if err != nil {
		return 0, err
	}
	return msg.MessageID, nil
}

func (c *TelegoClient) CopyMessage(ctx context.Context, req CopyMessageRequest) (int, error) {
	params := &telego.CopyMessageParams{
		ChatID:      chatID(req.ChatID),
		FromChatID:  chatID(req.FromChatID),
		MessageID:   req.MessageID,
		ReplyMarkup: toReplyMarkup(req.ReplyMarkup),
	}
	if req.ThreadID != nil {
		params.MessageThreadID = *req.ThreadID
	}
	if req.ReplyTo != nil {
		params.ReplyParameters = toReplyParameters(req.ReplyTo)
	}
	msgID, err := c.Bot.CopyMessage(ctx, params)
	if err != nil {
		return 0, err
	}
	return msgID.MessageID, nil
}

func (c *TelegoClient) EditMessageText(ctx context.Context, req EditTextRequest) error {
	_, err := c.Bot.EditMessageText(ctx, &telego.EditMessageTextParams{
		ChatID:    chatID(req.ChatID),
		MessageID: req.MessageID,
		Text:      req.Text,
		Entities:  req.Entities,
	})
	return err
}

func (c *TelegoClient) EditMessageCaption(ctx context.Context, req EditCaptionRequest) error {
	_, err := c.Bot.EditMessageCaption(ctx, &telego.EditMessageCaptionParams{
		ChatID:          chatID(req.ChatID),
		MessageID:       req.MessageID,
		Caption:         req.Caption,
		CaptionEntities: req.Entities,
	})
	return err
}

func (c *TelegoClient) DeleteMessage(ctx context.Context, chatIDValue int64, messageID int) error {
	return c.Bot.DeleteMessage(ctx, &telego.DeleteMessageParams{ChatID: chatID(chatIDValue), MessageID: messageID})
}

func (c *TelegoClient) CreateForumTopic(ctx context.Context, chatIDValue int64, name string) (int, error) {
	topic, err := c.Bot.CreateForumTopic(ctx, &telego.CreateForumTopicParams{ChatID: chatID(chatIDValue), Name: name})
	if err != nil {
		return 0, err
	}
	return topic.MessageThreadID, nil
}

func (c *TelegoClient) EditForumTopic(ctx context.Context, chatIDValue int64, threadID int, name string) error {
	return c.Bot.EditForumTopic(ctx, &telego.EditForumTopicParams{ChatID: chatID(chatIDValue), MessageThreadID: threadID, Name: name})
}

func (c *TelegoClient) AnswerCallback(ctx context.Context, queryID, text string, alert bool) error {
	return c.Bot.AnswerCallbackQuery(ctx, &telego.AnswerCallbackQueryParams{CallbackQueryID: queryID, Text: text, ShowAlert: alert})
}

func (c *TelegoClient) GetMe(ctx context.Context) (*telego.User, error) { return c.Bot.GetMe(ctx) }

func (c *TelegoClient) GetChat(ctx context.Context, chatIDValue int64) (*telego.ChatFullInfo, error) {
	return c.Bot.GetChat(ctx, &telego.GetChatParams{ChatID: chatID(chatIDValue)})
}

func (c *TelegoClient) GetChatMember(ctx context.Context, chatIDValue, userID int64) (telego.ChatMember, error) {
	return c.Bot.GetChatMember(ctx, &telego.GetChatMemberParams{ChatID: chatID(chatIDValue), UserID: userID})
}

func (c *TelegoClient) SetupProfile(ctx context.Context, cfg *config.Config) error {
	if cfg.BotName != "" {
		if err := c.Bot.SetMyName(ctx, &telego.SetMyNameParams{Name: trimWithLog("BOT_NAME", cfg.BotName, MaxBotNameLen)}); err != nil {
			return err
		}
	}
	if cfg.BotDescription != "" {
		if err := c.Bot.SetMyDescription(ctx, &telego.SetMyDescriptionParams{Description: trimWithLog("BOT_DESCRIPTION", cfg.BotDescription, MaxBotDescriptionLen)}); err != nil {
			return err
		}
	}
	if cfg.BotShortDescription != "" {
		if err := c.Bot.SetMyShortDescription(ctx, &telego.SetMyShortDescriptionParams{ShortDescription: trimWithLog("BOT_SHORT_DESCRIPTION", cfg.BotShortDescription, MaxBotShortDescriptionLen)}); err != nil {
			return err
		}
	}
	if len(cfg.BotUserCommands) > 0 {
		if err := c.Bot.SetMyCommands(ctx, &telego.SetMyCommandsParams{
			Commands: toBotCommands(cfg.BotUserCommands),
			Scope:    &telego.BotCommandScopeAllPrivateChats{Type: telego.ScopeTypeAllPrivateChats},
		}); err != nil {
			return err
		}
	}
	if len(cfg.BotAdminCommands) > 0 {
		for _, adminID := range cfg.AdminChatIDs {
			if err := c.Bot.SetMyCommands(ctx, &telego.SetMyCommandsParams{
				Commands: toBotCommands(cfg.BotAdminCommands),
				Scope:    &telego.BotCommandScopeChat{Type: telego.ScopeTypeChat, ChatID: chatID(adminID)},
			}); err != nil {
				return err
			}
		}
		if cfg.RelayMode == config.RelayModeGroupTopic && cfg.AdminGroupChatID != nil {
			if err := c.Bot.SetMyCommands(ctx, &telego.SetMyCommandsParams{
				Commands: toBotCommands(cfg.BotAdminCommands),
				Scope:    &telego.BotCommandScopeChat{Type: telego.ScopeTypeChat, ChatID: chatID(*cfg.AdminGroupChatID)},
			}); err != nil {
				return err
			}
		}
	}
	return nil
}

func toReplyMarkup(keyboard *InlineKeyboard) telego.ReplyMarkup {
	if keyboard == nil {
		return nil
	}
	return toInlineKeyboard(keyboard)
}

func ToInlineKeyboard(keyboard *InlineKeyboard) *telego.InlineKeyboardMarkup {
	return toInlineKeyboard(keyboard)
}

func toInlineKeyboard(keyboard *InlineKeyboard) *telego.InlineKeyboardMarkup {
	if keyboard == nil {
		return nil
	}
	rows := make([][]telego.InlineKeyboardButton, 0, len(*keyboard))
	for _, row := range *keyboard {
		buttons := make([]telego.InlineKeyboardButton, 0, len(row))
		for _, b := range row {
			buttons = append(buttons, telego.InlineKeyboardButton{Text: b.Text, CallbackData: b.Data})
		}
		rows = append(rows, buttons)
	}
	return &telego.InlineKeyboardMarkup{InlineKeyboard: rows}
}

func toReplyParameters(reply *ReplyRef) *telego.ReplyParameters {
	if reply == nil {
		return nil
	}
	return &telego.ReplyParameters{ChatID: chatID(reply.ChatID), MessageID: reply.MessageID, AllowSendingWithoutReply: true}
}

func toBotCommands(commands []config.Command) []telego.BotCommand {
	result := make([]telego.BotCommand, 0, len(commands))
	for _, cmd := range commands {
		result = append(result, telego.BotCommand{Command: cmd.Command, Description: cmd.Description})
	}
	return result
}

func chatID(id int64) telego.ChatID { return telego.ChatID{ID: id} }

func trimWithLog(label, value string, maxLen int) string {
	if len(value) <= maxLen {
		return value
	}
	log.Printf("%s 超出长度限制，已自动截断到 %d 字符。", label, maxLen)
	return value[:maxLen]
}

func ChatIDString(id int64) string { return fmt.Sprintf("%d", id) }

func IsCommandLike(text, caption string) bool {
	value := strings.TrimSpace(text)
	if value == "" {
		value = strings.TrimSpace(caption)
	}
	return strings.HasPrefix(value, "/")
}
