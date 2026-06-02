package domain

import (
	"fmt"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const PendingInputTimeout = 180 * time.Second

func UTCNowISO() string {
	return time.Now().UTC().Truncate(time.Second).Format(time.RFC3339)
}

func DisplayName(username, fullName string) string {
	if strings.TrimSpace(username) != "" {
		return fmt.Sprintf("%s (@%s)", fullName, username)
	}
	return fullName
}

func BuildUserTopicTitle(username, fullName string, userID int64) string {
	if strings.TrimSpace(username) != "" {
		return fmt.Sprintf("%s @%s (%d)", fullName, username, userID)
	}
	return fmt.Sprintf("%s (%d)", fullName, userID)
}

func ParseExpiryToken(raw string) *string {
	text := strings.ToLower(strings.TrimSpace(raw))
	if text == "" {
		return nil
	}
	durationRe := regexp.MustCompile(`^(\d+)([mhdw])$`)
	if matches := durationRe.FindStringSubmatch(text); matches != nil {
		amount, _ := strconv.Atoi(matches[1])
		if amount <= 0 {
			return nil
		}
		var delta time.Duration
		switch matches[2] {
		case "m":
			delta = time.Duration(amount) * time.Minute
		case "h":
			delta = time.Duration(amount) * time.Hour
		case "d":
			delta = time.Duration(amount) * 24 * time.Hour
		case "w":
			delta = time.Duration(amount) * 7 * 24 * time.Hour
		}
		value := time.Now().UTC().Add(delta).Truncate(time.Second).Format(time.RFC3339)
		return &value
	}
	if regexp.MustCompile(`^\d{4}-\d{2}-\d{2}$`).MatchString(text) {
		parsed, err := time.ParseInLocation("2006-01-02", text, time.UTC)
		if err != nil {
			return nil
		}
		value := parsed.UTC().Truncate(time.Second).Format(time.RFC3339)
		return &value
	}
	return nil
}

func FormatExpiryDisplay(expiresAt *string) string {
	if expiresAt == nil || *expiresAt == "" {
		return "永久"
	}
	expires, err := parseISOTime(*expiresAt)
	if err != nil {
		return *expiresAt
	}
	remaining := time.Until(expires)
	if remaining <= 0 {
		return "已过期"
	}
	days := int(remaining.Hours()) / 24
	hours := int(remaining.Hours()) % 24
	minutes := int(remaining.Minutes()) % 60
	if days > 0 {
		return fmt.Sprintf("%d天%d小时后", days, hours)
	}
	if hours > 0 {
		return fmt.Sprintf("%d小时%d分钟后", hours, minutes)
	}
	return fmt.Sprintf("%d分钟后", minutes)
}

func FormatUnbanTimeDisplay(expiresAt *string) string {
	if expiresAt == nil || *expiresAt == "" {
		return "永久"
	}
	expires, err := parseISOTime(*expiresAt)
	if err != nil {
		return "永久"
	}
	remaining := time.Until(expires)
	if remaining <= 0 {
		return "即将解封"
	}
	days := int(remaining.Hours()) / 24
	hours := int(remaining.Hours()) % 24
	minutes := int(remaining.Minutes()) % 60
	if days > 0 {
		return fmt.Sprintf("%d天%d小时", days, hours)
	}
	if hours > 0 {
		return fmt.Sprintf("%d小时%d分钟", hours, minutes)
	}
	if minutes < 1 {
		minutes = 1
	}
	return fmt.Sprintf("%d分钟", minutes)
}

func parseISOTime(value string) (time.Time, error) {
	parsed, err := time.Parse(time.RFC3339, value)
	if err == nil {
		return parsed, nil
	}
	return time.Parse("2006-01-02T15:04:05", value)
}

func ParseBanExtraArgs(args []string) (expiresAt *string, reason *string, note *string) {
	remaining := append([]string(nil), args...)
	if len(remaining) > 0 {
		if parsed := ParseExpiryToken(remaining[0]); parsed != nil {
			expiresAt = parsed
			remaining = remaining[1:]
		}
	}
	text := strings.TrimSpace(strings.Join(remaining, " "))
	if text == "" {
		return expiresAt, nil, nil
	}
	if strings.Contains(text, "|") {
		parts := strings.SplitN(text, "|", 2)
		reasonText := strings.TrimSpace(parts[0])
		noteText := strings.TrimSpace(parts[1])
		if reasonText != "" {
			reason = &reasonText
		}
		if noteText != "" {
			note = &noteText
		}
		return expiresAt, reason, note
	}
	reason = &text
	return expiresAt, reason, nil
}

func ParseRuleAddPayload(raw string) (triggerType, triggerText, replyText string, ok bool) {
	if !strings.Contains(raw, "=>") {
		return "", "", "", false
	}
	parts := strings.SplitN(raw, "=>", 2)
	left := strings.TrimSpace(parts[0])
	replyText = strings.TrimSpace(parts[1])
	if left == "" || replyText == "" || !strings.Contains(left, " ") {
		return "", "", "", false
	}
	leftParts := strings.SplitN(left, " ", 2)
	aliases := map[string]string{
		"exact": "exact", "精确": "exact", "精准": "exact",
		"contains": "contains", "包含": "contains",
		"prefix": "prefix", "前缀": "prefix",
		"regex": "regex", "正则": "regex",
	}
	triggerType = aliases[strings.ToLower(strings.TrimSpace(leftParts[0]))]
	triggerText = strings.TrimSpace(leftParts[1])
	if triggerType == "" || triggerText == "" {
		return "", "", "", false
	}
	return triggerType, triggerText, replyText, true
}

func ParseStatsWindow(arg string) (string, time.Time) {
	text := strings.ToLower(strings.TrimSpace(arg))
	now := time.Now().UTC()
	switch text {
	case "7d":
		return "7d", now.Add(-7 * 24 * time.Hour)
	case "30d":
		return "30d", now.Add(-30 * 24 * time.Hour)
	default:
		return "24h", now.Add(-24 * time.Hour)
	}
}
