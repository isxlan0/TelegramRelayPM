package store

import (
	"context"
	"database/sql"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"time"

	"telegramrelaypm/internal/domain"

	_ "modernc.org/sqlite"
)

type User struct {
	UserID       int64
	Username     string
	FullName     string
	LastActiveAt string
}

type MessageMap struct {
	ID             int64
	UserChatID     int64
	AdminChatID    int64
	UserMessageID  int
	AdminMessageID int
	Direction      string
	CreatedAt      string
}

type UserTopic struct {
	UserID           int64
	AdminGroupChatID int64
	TopicThreadID    int
	TopicTitle       string
	CreatedAt        string
	UpdatedAt        string
}

type Ban struct {
	UserID          int64
	CreatedAt       string
	UpdatedAt       string
	OperatorAdminID int64
	Reason          string
	Note            string
	ExpiresAt       *string
}

type Rule struct {
	ID               int64
	TriggerType      string
	TriggerText      string
	ReplyText        string
	Priority         int
	IsEnabled        bool
	CreatedByAdminID int64
	CreatedAt        string
	UpdatedAt        string
}

type AuditEvent struct {
	EventType       string
	Outcome         string
	UserID          *int64
	AdminChatID     *int64
	ChatID          *int64
	MessageID       *int
	MappedMessageID *int
	MessageKind     *string
	IsEdited        bool
	Direction       *string
	ErrorClass      *string
	ErrorCode       *string
}

type StatCount struct {
	EventType string
	Outcome   string
	Count     int
}

type TopUser struct {
	UserID int64
	Count  int
}

type SQLite struct {
	db             *sql.DB
	mu             sync.RWMutex
	primaryAdminID int64
}

func OpenSQLite(ctx context.Context, path string, primaryAdminID int64) (*SQLite, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	s := &SQLite{db: db, primaryAdminID: primaryAdminID}
	if err := s.initSchema(ctx); err != nil {
		_ = db.Close()
		return nil, err
	}
	return s, nil
}

func (s *SQLite) Close() error { return s.db.Close() }

func (s *SQLite) initSchema(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	_, err := s.db.ExecContext(ctx, `
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_chat_id INTEGER NOT NULL,
    admin_chat_id INTEGER NOT NULL,
    user_message_id INTEGER NOT NULL,
    admin_message_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_map_admin_msg ON message_map(admin_chat_id, admin_message_id);
CREATE INDEX IF NOT EXISTS idx_message_map_user_msg ON message_map(user_chat_id, user_message_id);

CREATE TABLE IF NOT EXISTS admin_state (
    admin_chat_id INTEGER PRIMARY KEY,
    current_session_user_id INTEGER
);

CREATE TABLE IF NOT EXISTS user_topics (
    user_id INTEGER PRIMARY KEY,
    admin_group_chat_id INTEGER NOT NULL,
    topic_thread_id INTEGER NOT NULL,
    topic_title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_user_topics_group_thread ON user_topics(admin_group_chat_id, topic_thread_id);

CREATE TABLE IF NOT EXISTS banned_users (
    user_id INTEGER PRIMARY KEY,
    banned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ban_list (
    user_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    operator_admin_id INTEGER NOT NULL,
    reason TEXT,
    note TEXT,
    expires_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_ban_list_expires_at ON ban_list(expires_at);

CREATE TABLE IF NOT EXISTS auto_reply_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,
    trigger_text TEXT NOT NULL,
    reply_text TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    is_enabled INTEGER NOT NULL DEFAULT 1,
    created_by_admin_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auto_reply_rules_enabled_priority ON auto_reply_rules(is_enabled, priority, id);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    user_id INTEGER,
    admin_chat_id INTEGER,
    chat_id INTEGER,
    message_id INTEGER,
    mapped_message_id INTEGER,
    message_kind TEXT,
    is_edited INTEGER NOT NULL DEFAULT 0,
    direction TEXT,
    outcome TEXT NOT NULL,
    error_class TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_type_time ON audit_events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_events_user_time ON audit_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_events_admin_time ON audit_events(admin_chat_id, created_at);
`)
	if err != nil {
		return err
	}
	return s.migrateBanListFromLegacyLocked(ctx)
}

func (s *SQLite) migrateBanListFromLegacyLocked(ctx context.Context) error {
	rows, err := s.db.QueryContext(ctx, `SELECT user_id, banned_at FROM banned_users`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var userID int64
		var bannedAt string
		if err := rows.Scan(&userID, &bannedAt); err != nil {
			return err
		}
		_, err := s.db.ExecContext(ctx, `
INSERT INTO ban_list (user_id, created_at, updated_at, operator_admin_id, reason, note, expires_at)
SELECT ?, ?, ?, ?, NULL, NULL, NULL
WHERE NOT EXISTS (SELECT 1 FROM ban_list WHERE user_id = ?)
`, userID, bannedAt, bannedAt, s.primaryAdminID, userID)
		if err != nil {
			return err
		}
	}
	return rows.Err()
}

func (s *SQLite) TouchUser(ctx context.Context, userID int64, username, fullName string) error {
	now := domain.UTCNowISO()
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx, `
INSERT INTO users (user_id, username, full_name, first_seen_at, last_active_at)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(user_id) DO UPDATE SET
    username=excluded.username,
    full_name=excluded.full_name,
    last_active_at=excluded.last_active_at
`, userID, nullString(username), fullName, now, now)
	return err
}

func (s *SQLite) SaveMapping(ctx context.Context, m MessageMap) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx, `
INSERT INTO message_map (user_chat_id, admin_chat_id, user_message_id, admin_message_id, direction, created_at)
VALUES (?, ?, ?, ?, ?, ?)
`, m.UserChatID, m.AdminChatID, m.UserMessageID, m.AdminMessageID, m.Direction, domain.UTCNowISO())
	return err
}

func (s *SQLite) GetTargetUserByAdminMessage(ctx context.Context, adminChatID int64, adminMessageID int) (*int64, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var userID int64
	err := s.db.QueryRowContext(ctx, `
SELECT user_chat_id FROM message_map
WHERE admin_chat_id = ? AND admin_message_id = ?
ORDER BY id DESC LIMIT 1
`, adminChatID, adminMessageID).Scan(&userID)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &userID, nil
}

func (s *SQLite) GetUserToAdminMaps(ctx context.Context, userChatID int64, userMessageID int) ([]MessageMap, error) {
	return s.queryMaps(ctx, `
SELECT id, user_chat_id, admin_chat_id, user_message_id, admin_message_id, direction, created_at
FROM message_map
WHERE user_chat_id = ? AND user_message_id = ? AND direction = 'user_to_admin'
ORDER BY id DESC
`, userChatID, userMessageID)
}

func (s *SQLite) GetAdminToUserMaps(ctx context.Context, adminChatID int64, adminMessageID int) ([]MessageMap, error) {
	return s.queryMaps(ctx, `
SELECT id, user_chat_id, admin_chat_id, user_message_id, admin_message_id, direction, created_at
FROM message_map
WHERE admin_chat_id = ? AND admin_message_id = ? AND direction IN ('admin_to_user', 'broadcast')
ORDER BY id DESC
`, adminChatID, adminMessageID)
}

func (s *SQLite) GetMapsByAdminMessage(ctx context.Context, adminChatID int64, adminMessageID int) ([]MessageMap, error) {
	return s.queryMaps(ctx, `
SELECT id, user_chat_id, admin_chat_id, user_message_id, admin_message_id, direction, created_at
FROM message_map
WHERE admin_chat_id = ? AND admin_message_id = ?
ORDER BY id DESC
`, adminChatID, adminMessageID)
}

func (s *SQLite) queryMaps(ctx context.Context, query string, args ...any) ([]MessageMap, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	maps := []MessageMap{}
	for rows.Next() {
		var m MessageMap
		if err := rows.Scan(&m.ID, &m.UserChatID, &m.AdminChatID, &m.UserMessageID, &m.AdminMessageID, &m.Direction, &m.CreatedAt); err != nil {
			return nil, err
		}
		maps = append(maps, m)
	}
	return maps, rows.Err()
}

func (s *SQLite) GetRecentUsers(ctx context.Context, limit int, excludeUserID *int64) ([]User, error) {
	if limit <= 0 {
		limit = 10
	}
	query := `SELECT user_id, username, full_name, last_active_at FROM users ORDER BY last_active_at DESC LIMIT ?`
	args := []any{limit}
	if excludeUserID != nil {
		query = `SELECT user_id, username, full_name, last_active_at FROM users WHERE user_id != ? ORDER BY last_active_at DESC LIMIT ?`
		args = []any{*excludeUserID, limit}
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	users := []User{}
	for rows.Next() {
		var u User
		var username sql.NullString
		if err := rows.Scan(&u.UserID, &username, &u.FullName, &u.LastActiveAt); err != nil {
			return nil, err
		}
		u.Username = username.String
		users = append(users, u)
	}
	return users, rows.Err()
}

func (s *SQLite) GetAllUsers(ctx context.Context, excludeUserID *int64) ([]int64, error) {
	query := `SELECT user_id FROM users ORDER BY last_active_at DESC`
	args := []any{}
	if excludeUserID != nil {
		query = `SELECT user_id FROM users WHERE user_id != ? ORDER BY last_active_at DESC`
		args = append(args, *excludeUserID)
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	ids := []int64{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func (s *SQLite) SetCurrentSession(ctx context.Context, adminChatID int64, userID *int64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx, `
INSERT INTO admin_state (admin_chat_id, current_session_user_id) VALUES (?, ?)
ON CONFLICT(admin_chat_id) DO UPDATE SET current_session_user_id = excluded.current_session_user_id
`, adminChatID, nullableInt64(userID))
	return err
}

func (s *SQLite) GetCurrentSession(ctx context.Context, adminChatID int64) (*int64, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var value sql.NullInt64
	err := s.db.QueryRowContext(ctx, `SELECT current_session_user_id FROM admin_state WHERE admin_chat_id = ?`, adminChatID).Scan(&value)
	if err == sql.ErrNoRows || !value.Valid {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &value.Int64, nil
}

func (s *SQLite) GetUserTopic(ctx context.Context, userID int64) (*UserTopic, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var t UserTopic
	err := s.db.QueryRowContext(ctx, `SELECT user_id, admin_group_chat_id, topic_thread_id, topic_title, created_at, updated_at FROM user_topics WHERE user_id = ? LIMIT 1`, userID).Scan(&t.UserID, &t.AdminGroupChatID, &t.TopicThreadID, &t.TopicTitle, &t.CreatedAt, &t.UpdatedAt)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &t, nil
}

func (s *SQLite) UpsertUserTopic(ctx context.Context, t UserTopic) error {
	now := domain.UTCNowISO()
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx, `
INSERT INTO user_topics (user_id, admin_group_chat_id, topic_thread_id, topic_title, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id) DO UPDATE SET
    admin_group_chat_id = excluded.admin_group_chat_id,
    topic_thread_id = excluded.topic_thread_id,
    topic_title = excluded.topic_title,
    updated_at = excluded.updated_at
`, t.UserID, t.AdminGroupChatID, t.TopicThreadID, t.TopicTitle, now, now)
	return err
}

func (s *SQLite) UpdateUserTopicTitle(ctx context.Context, userID int64, title string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx, `UPDATE user_topics SET topic_title = ?, updated_at = ? WHERE user_id = ?`, title, domain.UTCNowISO(), userID)
	return err
}

func (s *SQLite) GetUserIDByTopic(ctx context.Context, adminGroupChatID int64, topicThreadID int) (*int64, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	var userID int64
	err := s.db.QueryRowContext(ctx, `SELECT user_id FROM user_topics WHERE admin_group_chat_id = ? AND topic_thread_id = ? LIMIT 1`, adminGroupChatID, topicThreadID).Scan(&userID)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &userID, nil
}

func (s *SQLite) BanUser(ctx context.Context, userID, operatorAdminID int64, reason, note, expiresAt *string) error {
	now := domain.UTCNowISO()
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx, `
INSERT INTO ban_list (user_id, created_at, updated_at, operator_admin_id, reason, note, expires_at)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(user_id) DO UPDATE SET
    updated_at=excluded.updated_at,
    operator_admin_id=excluded.operator_admin_id,
    reason=excluded.reason,
    note=excluded.note,
    expires_at=excluded.expires_at
`, userID, now, now, operatorAdminID, nullableString(reason), nullableString(note), nullableString(expiresAt))
	if err != nil {
		return err
	}
	_, err = s.db.ExecContext(ctx, `INSERT OR REPLACE INTO banned_users (user_id, banned_at) VALUES (?, ?)`, userID, now)
	return err
}

func (s *SQLite) UnbanUser(ctx context.Context, userID int64) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.ExecContext(ctx, `DELETE FROM ban_list WHERE user_id = ?`, userID)
	if err != nil {
		return false, err
	}
	_, err = s.db.ExecContext(ctx, `DELETE FROM banned_users WHERE user_id = ?`, userID)
	if err != nil {
		return false, err
	}
	count, _ := res.RowsAffected()
	return count > 0, nil
}

func (s *SQLite) GetBan(ctx context.Context, userID int64) (*Ban, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ban, err := s.getBanLocked(ctx, userID)
	return ban, err
}

func (s *SQLite) getBanLocked(ctx context.Context, userID int64) (*Ban, error) {
	var b Ban
	var reason, note, expires sql.NullString
	err := s.db.QueryRowContext(ctx, `SELECT user_id, created_at, updated_at, operator_admin_id, reason, note, expires_at FROM ban_list WHERE user_id = ? LIMIT 1`, userID).Scan(&b.UserID, &b.CreatedAt, &b.UpdatedAt, &b.OperatorAdminID, &reason, &note, &expires)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	b.Reason = reason.String
	b.Note = note.String
	if expires.Valid && expires.String != "" {
		b.ExpiresAt = &expires.String
		if expiredISO(expires.String) {
			_, _ = s.db.ExecContext(ctx, `DELETE FROM ban_list WHERE user_id = ?`, userID)
			_, _ = s.db.ExecContext(ctx, `DELETE FROM banned_users WHERE user_id = ?`, userID)
			return nil, nil
		}
	}
	return &b, nil
}

func (s *SQLite) IsUserBanned(ctx context.Context, userID int64) (bool, error) {
	b, err := s.GetBan(ctx, userID)
	return b != nil, err
}

func (s *SQLite) ListActiveBans(ctx context.Context, limit int) ([]Ban, error) {
	if limit <= 0 {
		limit = 20
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	rows, err := s.db.QueryContext(ctx, `SELECT user_id FROM ban_list ORDER BY updated_at DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	ids := []int64{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	bans := []Ban{}
	for _, id := range ids {
		b, err := s.getBanLocked(ctx, id)
		if err != nil {
			return nil, err
		}
		if b != nil {
			bans = append(bans, *b)
		}
	}
	return bans, nil
}

func (s *SQLite) AddAutoReplyRule(ctx context.Context, triggerType, triggerText, replyText string, priority int, createdByAdminID int64) (int64, error) {
	now := domain.UTCNowISO()
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.ExecContext(ctx, `
INSERT INTO auto_reply_rules (trigger_type, trigger_text, reply_text, priority, is_enabled, created_by_admin_id, created_at, updated_at)
VALUES (?, ?, ?, ?, 1, ?, ?, ?)
`, triggerType, triggerText, replyText, priority, createdByAdminID, now, now)
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

func (s *SQLite) ListAutoReplyRules(ctx context.Context, limit int) ([]Rule, error) {
	if limit <= 0 {
		limit = 50
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx, `SELECT id, trigger_type, trigger_text, reply_text, priority, is_enabled, created_by_admin_id, created_at, updated_at FROM auto_reply_rules ORDER BY priority ASC, id ASC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	rules := []Rule{}
	for rows.Next() {
		r, err := scanRule(rows)
		if err != nil {
			return nil, err
		}
		rules = append(rules, r)
	}
	return rules, rows.Err()
}

func (s *SQLite) SetAutoReplyRuleEnabled(ctx context.Context, ruleID int64, enabled bool) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.ExecContext(ctx, `UPDATE auto_reply_rules SET is_enabled = ?, updated_at = ? WHERE id = ?`, boolInt(enabled), domain.UTCNowISO(), ruleID)
	if err != nil {
		return false, err
	}
	count, _ := res.RowsAffected()
	return count > 0, nil
}

func (s *SQLite) DeleteAutoReplyRule(ctx context.Context, ruleID int64) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.ExecContext(ctx, `DELETE FROM auto_reply_rules WHERE id = ?`, ruleID)
	if err != nil {
		return false, err
	}
	count, _ := res.RowsAffected()
	return count > 0, nil
}

func (s *SQLite) MatchAutoReplyRule(ctx context.Context, text string) (*Rule, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx, `SELECT id, trigger_type, trigger_text, reply_text, priority, is_enabled, created_by_admin_id, created_at, updated_at FROM auto_reply_rules WHERE is_enabled = 1 ORDER BY priority ASC, id ASC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	candidate := strings.TrimSpace(text)
	for rows.Next() {
		r, err := scanRule(rows)
		if err != nil {
			return nil, err
		}
		matched := false
		switch r.TriggerType {
		case "exact":
			matched = candidate == r.TriggerText
		case "contains":
			matched = strings.Contains(candidate, r.TriggerText)
		case "prefix":
			matched = strings.HasPrefix(candidate, r.TriggerText)
		case "regex":
			matched, _ = regexp.MatchString(r.TriggerText, candidate)
		}
		if matched {
			return &r, nil
		}
	}
	return nil, rows.Err()
}

func (s *SQLite) RecordAuditEvent(ctx context.Context, e AuditEvent) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, err := s.db.ExecContext(ctx, `
INSERT INTO audit_events (event_type, user_id, admin_chat_id, chat_id, message_id, mapped_message_id, message_kind, is_edited, direction, outcome, error_class, error_code, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
`, e.EventType, nullableInt64(e.UserID), nullableInt64(e.AdminChatID), nullableInt64(e.ChatID), nullableInt(e.MessageID), nullableInt(e.MappedMessageID), nullableString(e.MessageKind), boolInt(e.IsEdited), nullableString(e.Direction), e.Outcome, nullableString(e.ErrorClass), nullableString(e.ErrorCode), domain.UTCNowISO())
	return err
}

func (s *SQLite) GetStatsCounts(ctx context.Context, sinceISO string) ([]StatCount, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx, `SELECT event_type, outcome, COUNT(*) AS cnt FROM audit_events WHERE created_at >= ? GROUP BY event_type, outcome`, sinceISO)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	counts := []StatCount{}
	for rows.Next() {
		var c StatCount
		if err := rows.Scan(&c.EventType, &c.Outcome, &c.Count); err != nil {
			return nil, err
		}
		counts = append(counts, c)
	}
	return counts, rows.Err()
}

func (s *SQLite) GetTopUsersByEvents(ctx context.Context, sinceISO string, limit int) ([]TopUser, error) {
	if limit <= 0 {
		limit = 10
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	rows, err := s.db.QueryContext(ctx, `SELECT user_id, COUNT(*) AS cnt FROM audit_events WHERE created_at >= ? AND user_id IS NOT NULL GROUP BY user_id ORDER BY cnt DESC LIMIT ?`, sinceISO, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	top := []TopUser{}
	for rows.Next() {
		var u TopUser
		if err := rows.Scan(&u.UserID, &u.Count); err != nil {
			return nil, err
		}
		top = append(top, u)
	}
	return top, rows.Err()
}

func (s *SQLite) DeleteMappingsByAdminMessage(ctx context.Context, adminChatID int64, adminMessageID int) (int64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	res, err := s.db.ExecContext(ctx, `DELETE FROM message_map WHERE admin_chat_id = ? AND admin_message_id = ?`, adminChatID, adminMessageID)
	if err != nil {
		return 0, err
	}
	return res.RowsAffected()
}

func scanRule(scanner interface{ Scan(dest ...any) error }) (Rule, error) {
	var r Rule
	var enabled int
	err := scanner.Scan(&r.ID, &r.TriggerType, &r.TriggerText, &r.ReplyText, &r.Priority, &enabled, &r.CreatedByAdminID, &r.CreatedAt, &r.UpdatedAt)
	r.IsEnabled = enabled != 0
	return r, err
}

func nullString(value string) sql.NullString {
	if strings.TrimSpace(value) == "" {
		return sql.NullString{}
	}
	return sql.NullString{String: value, Valid: true}
}

func nullableString(value *string) any {
	if value == nil {
		return nil
	}
	return *value
}

func nullableInt64(value *int64) any {
	if value == nil {
		return nil
	}
	return *value
}

func nullableInt(value *int) any {
	if value == nil {
		return nil
	}
	return *value
}

func boolInt(value bool) int {
	if value {
		return 1
	}
	return 0
}

func expiredISO(value string) bool {
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil {
		parsed, err = time.Parse("2006-01-02T15:04:05", value)
	}
	return err == nil && !parsed.After(time.Now().UTC())
}

func FormatBanInfo(b Ban) string {
	reason := strings.TrimSpace(b.Reason)
	if reason == "" {
		reason = "-"
	}
	note := strings.TrimSpace(b.Note)
	if note == "" {
		note = "-"
	}
	return fmt.Sprintf("封禁信息：\n- 用户ID：%d\n- 原因：%s\n- 备注：%s\n- 到期：%s\n- 操作管理员：%d\n- 创建时间：%s\n- 更新时间：%s",
		b.UserID, reason, note, domain.FormatExpiryDisplay(b.ExpiresAt), b.OperatorAdminID, b.CreatedAt, b.UpdatedAt)
}
