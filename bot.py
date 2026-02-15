import asyncio
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from telegram import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config

MAX_BOT_NAME_LEN = 64
MAX_BOT_DESCRIPTION_LEN = 512
MAX_BOT_SHORT_DESCRIPTION_LEN = 120


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def display_name(username: Optional[str], full_name: str) -> str:
    if username:
        return f"{full_name} (@{username})"
    return full_name


def build_user_topic_title(username: Optional[str], full_name: str, user_id: int) -> str:
    if username:
        return f"{full_name} @{username} ({user_id})"
    return f"{full_name} ({user_id})"


def trim_with_log(label: str, value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    logging.warning("%s 超出长度限制，已自动截断到 %d 字符。", label, max_len)
    return value[:max_len]


def parse_expiry_token(raw: str) -> Optional[str]:
    text = raw.strip().lower()
    if not text:
        return None

    duration_match = re.fullmatch(r"(\d+)([mhdw])", text)
    if duration_match:
        amount = int(duration_match.group(1))
        unit = duration_match.group(2)
        if amount <= 0:
            return None
        delta = {
            "m": timedelta(minutes=amount),
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
        }[unit]
        return (datetime.now(timezone.utc) + delta).replace(microsecond=0).isoformat()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            as_date = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return as_date.replace(microsecond=0).isoformat()

    return None


def format_expiry_display(expires_at: Optional[str]) -> str:
    if not expires_at:
        return "永久"
    try:
        expires_dt = datetime.fromisoformat(expires_at)
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return expires_at

    remaining = expires_dt - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return "已过期"

    total_seconds = int(remaining.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}天{hours}小时后"
    if hours > 0:
        return f"{hours}小时{minutes}分钟后"
    return f"{minutes}分钟后"


def format_unban_time_display(expires_at: Optional[str]) -> str:
    if not expires_at:
        return "永久"
    try:
        expires_dt = datetime.fromisoformat(expires_at)
        if expires_dt.tzinfo is None:
            expires_dt = expires_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return "永久"

    remaining = expires_dt - datetime.now(timezone.utc)
    if remaining.total_seconds() <= 0:
        return "即将解封"

    total_seconds = int(remaining.total_seconds())
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}天{hours}小时"
    if hours > 0:
        return f"{hours}小时{minutes}分钟"
    return f"{max(1, minutes)}分钟"


def message_kind(message) -> str:
    if message.text is not None:
        return "text"
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.document:
        return "document"
    if message.audio:
        return "audio"
    if message.voice:
        return "voice"
    if message.sticker:
        return "sticker"
    if message.animation:
        return "animation"
    if message.location:
        return "location"
    if message.contact:
        return "contact"
    return "other"


def format_ban_info(row: sqlite3.Row) -> str:
    user_id = int(row["user_id"])
    reason = (row["reason"] or "-").strip()
    note = (row["note"] or "-").strip()
    expires_at = row["expires_at"]
    operator_admin_id = int(row["operator_admin_id"])
    created_at = row["created_at"]
    updated_at = row["updated_at"]

    return (
        "封禁信息：\n"
        f"- 用户ID：{user_id}\n"
        f"- 原因：{reason}\n"
        f"- 备注：{note}\n"
        f"- 到期：{format_expiry_display(expires_at)}\n"
        f"- 操作管理员：{operator_admin_id}\n"
        f"- 创建时间：{created_at}\n"
        f"- 更新时间：{updated_at}"
    )


class RelayDB:
    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._init_schema()

    def _init_schema(self) -> None:
        with self.lock:
            self.conn.executescript(
                """
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

            CREATE INDEX IF NOT EXISTS idx_message_map_admin_msg
            ON message_map(admin_chat_id, admin_message_id);

            CREATE INDEX IF NOT EXISTS idx_message_map_user_msg
            ON message_map(user_chat_id, user_message_id);

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

            CREATE UNIQUE INDEX IF NOT EXISTS idx_user_topics_group_thread
            ON user_topics(admin_group_chat_id, topic_thread_id);

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

            CREATE INDEX IF NOT EXISTS idx_ban_list_expires_at
            ON ban_list(expires_at);

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

            CREATE INDEX IF NOT EXISTS idx_auto_reply_rules_enabled_priority
            ON auto_reply_rules(is_enabled, priority, id);

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

            CREATE INDEX IF NOT EXISTS idx_audit_events_type_time
            ON audit_events(event_type, created_at);

            CREATE INDEX IF NOT EXISTS idx_audit_events_user_time
            ON audit_events(user_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_audit_events_admin_time
            ON audit_events(admin_chat_id, created_at);
            """
            )
            self._migrate_ban_list_from_legacy()
            self.conn.commit()

    def _migrate_ban_list_from_legacy(self) -> None:
        rows = self.conn.execute("SELECT user_id, banned_at FROM banned_users").fetchall()
        for row in rows:
            user_id = int(row["user_id"])
            existing = self.conn.execute(
                "SELECT 1 FROM ban_list WHERE user_id = ? LIMIT 1", (user_id,)
            ).fetchone()
            if existing:
                continue
            banned_at = str(row["banned_at"])
            self.conn.execute(
                """
                INSERT INTO ban_list (
                    user_id, created_at, updated_at, operator_admin_id, reason, note, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, banned_at, banned_at, config.PRIMARY_ADMIN_CHAT_ID, None, None, None),
            )

    def touch_user(self, user_id: int, username: Optional[str], full_name: str) -> None:
        now = utc_now_iso()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, first_seen_at, last_active_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    full_name=excluded.full_name,
                    last_active_at=excluded.last_active_at
                """,
                (user_id, username, full_name, now, now),
            )
            self.conn.commit()

    def save_mapping(
        self,
        user_chat_id: int,
        admin_chat_id: int,
        user_message_id: int,
        admin_message_id: int,
        direction: str,
    ) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO message_map (
                    user_chat_id, admin_chat_id, user_message_id, admin_message_id, direction, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    user_chat_id,
                    admin_chat_id,
                    user_message_id,
                    admin_message_id,
                    direction,
                    utc_now_iso(),
                ),
            )
            self.conn.commit()

    def get_target_user_by_admin_message(
        self, admin_chat_id: int, admin_message_id: int
    ) -> Optional[int]:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT user_chat_id
                FROM message_map
                WHERE admin_chat_id = ? AND admin_message_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (admin_chat_id, admin_message_id),
            ).fetchone()
        return int(row["user_chat_id"]) if row else None

    def get_user_to_admin_map(
        self, user_chat_id: int, user_message_id: int
    ) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM message_map
                WHERE user_chat_id = ?
                  AND user_message_id = ?
                  AND direction = 'user_to_admin'
                ORDER BY id DESC
                LIMIT 1
                """,
                (user_chat_id, user_message_id),
            ).fetchone()

    def get_user_to_admin_maps(
        self, user_chat_id: int, user_message_id: int
    ):
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM message_map
                WHERE user_chat_id = ?
                  AND user_message_id = ?
                  AND direction = 'user_to_admin'
                ORDER BY id DESC
                """,
                (user_chat_id, user_message_id),
            ).fetchall()

    def get_admin_to_user_maps(self, admin_chat_id: int, admin_message_id: int):
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM message_map
                WHERE admin_chat_id = ?
                  AND admin_message_id = ?
                  AND direction IN ('admin_to_user', 'broadcast')
                ORDER BY id DESC
                """,
                (admin_chat_id, admin_message_id),
            ).fetchall()

    def get_maps_by_admin_message(self, admin_chat_id: int, admin_message_id: int):
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM message_map
                WHERE admin_chat_id = ?
                  AND admin_message_id = ?
                ORDER BY id DESC
                """,
                (admin_chat_id, admin_message_id),
            ).fetchall()

    def get_recent_users(self, limit_count: int, exclude_user_id: Optional[int] = None):
        with self.lock:
            if exclude_user_id is None:
                return self.conn.execute(
                    """
                    SELECT user_id, username, full_name, last_active_at
                    FROM users
                    ORDER BY last_active_at DESC
                    LIMIT ?
                    """,
                    (limit_count,),
                ).fetchall()
            return self.conn.execute(
                """
                SELECT user_id, username, full_name, last_active_at
                FROM users
                WHERE user_id != ?
                ORDER BY last_active_at DESC
                LIMIT ?
                """,
                (exclude_user_id, limit_count),
            ).fetchall()

    def get_all_users(self, exclude_user_id: Optional[int] = None):
        with self.lock:
            if exclude_user_id is None:
                return self.conn.execute(
                    "SELECT user_id FROM users ORDER BY last_active_at DESC"
                ).fetchall()
            return self.conn.execute(
                "SELECT user_id FROM users WHERE user_id != ? ORDER BY last_active_at DESC",
                (exclude_user_id,),
            ).fetchall()

    def set_current_session(self, admin_chat_id: int, user_id: Optional[int]) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO admin_state (admin_chat_id, current_session_user_id)
                VALUES (?, ?)
                ON CONFLICT(admin_chat_id) DO UPDATE SET
                    current_session_user_id = excluded.current_session_user_id
                """,
                (admin_chat_id, user_id),
            )
            self.conn.commit()

    def get_current_session(self, admin_chat_id: int) -> Optional[int]:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT current_session_user_id
                FROM admin_state
                WHERE admin_chat_id = ?
                """,
                (admin_chat_id,),
            ).fetchone()
        if not row:
            return None
        return row["current_session_user_id"]

    def get_user_topic(self, user_id: int) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM user_topics
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()

    def upsert_user_topic(
        self,
        user_id: int,
        admin_group_chat_id: int,
        topic_thread_id: int,
        topic_title: str,
    ) -> None:
        now = utc_now_iso()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO user_topics (
                    user_id, admin_group_chat_id, topic_thread_id, topic_title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    admin_group_chat_id = excluded.admin_group_chat_id,
                    topic_thread_id = excluded.topic_thread_id,
                    topic_title = excluded.topic_title,
                    updated_at = excluded.updated_at
                """,
                (user_id, admin_group_chat_id, topic_thread_id, topic_title, now, now),
            )
            self.conn.commit()

    def update_user_topic_title(self, user_id: int, topic_title: str) -> None:
        with self.lock:
            self.conn.execute(
                """
                UPDATE user_topics
                SET topic_title = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (topic_title, utc_now_iso(), user_id),
            )
            self.conn.commit()

    def get_user_id_by_topic(self, admin_group_chat_id: int, topic_thread_id: int) -> Optional[int]:
        with self.lock:
            row = self.conn.execute(
                """
                SELECT user_id
                FROM user_topics
                WHERE admin_group_chat_id = ? AND topic_thread_id = ?
                LIMIT 1
                """,
                (admin_group_chat_id, topic_thread_id),
            ).fetchone()
        return int(row["user_id"]) if row else None

    def ban_user(
        self,
        user_id: int,
        operator_admin_id: int,
        reason: Optional[str] = None,
        note: Optional[str] = None,
        expires_at: Optional[str] = None,
    ) -> None:
        now = utc_now_iso()
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO ban_list (user_id, created_at, updated_at, operator_admin_id, reason, note, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    operator_admin_id=excluded.operator_admin_id,
                    reason=excluded.reason,
                    note=excluded.note,
                    expires_at=excluded.expires_at
                """,
                (user_id, now, now, operator_admin_id, reason, note, expires_at),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO banned_users (user_id, banned_at) VALUES (?, ?)",
                (user_id, now),
            )
            self.conn.commit()

    def unban_user(self, user_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM ban_list WHERE user_id = ?", (user_id,))
            self.conn.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def get_ban(self, user_id: int) -> Optional[sqlite3.Row]:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM ban_list WHERE user_id = ? LIMIT 1",
                (user_id,),
            ).fetchone()
            if not row:
                return None

            expires_at = row["expires_at"]
            if expires_at:
                try:
                    expires_dt = datetime.fromisoformat(expires_at)
                    if expires_dt.tzinfo is None:
                        expires_dt = expires_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    expires_dt = None
                if expires_dt and expires_dt <= datetime.now(timezone.utc):
                    self.conn.execute("DELETE FROM ban_list WHERE user_id = ?", (user_id,))
                    self.conn.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
                    self.conn.commit()
                    return None

            return row

    def is_user_banned(self, user_id: int) -> bool:
        return self.get_ban(user_id) is not None

    def list_active_bans(self, limit_count: int):
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT *
                FROM ban_list
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit_count,),
            ).fetchall()

        active = []
        for row in rows:
            refreshed = self.get_ban(int(row["user_id"]))
            if refreshed is not None:
                active.append(refreshed)
        return active

    def add_auto_reply_rule(
        self,
        trigger_type: str,
        trigger_text: str,
        reply_text: str,
        priority: int,
        created_by_admin_id: int,
    ) -> int:
        now = utc_now_iso()
        with self.lock:
            cur = self.conn.execute(
                """
                INSERT INTO auto_reply_rules (
                    trigger_type, trigger_text, reply_text, priority, is_enabled,
                    created_by_admin_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (trigger_type, trigger_text, reply_text, priority, created_by_admin_id, now, now),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def list_auto_reply_rules(self, limit_count: int = 50):
        with self.lock:
            return self.conn.execute(
                """
                SELECT *
                FROM auto_reply_rules
                ORDER BY priority ASC, id ASC
                LIMIT ?
                """,
                (limit_count,),
            ).fetchall()

    def set_auto_reply_rule_enabled(self, rule_id: int, enabled: bool) -> bool:
        now = utc_now_iso()
        with self.lock:
            cur = self.conn.execute(
                """
                UPDATE auto_reply_rules
                SET is_enabled = ?, updated_at = ?
                WHERE id = ?
                """,
                (1 if enabled else 0, now, rule_id),
            )
            self.conn.commit()
            return cur.rowcount > 0

    def delete_auto_reply_rule(self, rule_id: int) -> bool:
        with self.lock:
            cur = self.conn.execute("DELETE FROM auto_reply_rules WHERE id = ?", (rule_id,))
            self.conn.commit()
            return cur.rowcount > 0

    def match_auto_reply_rule(self, text: str) -> Optional[sqlite3.Row]:
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT *
                FROM auto_reply_rules
                WHERE is_enabled = 1
                ORDER BY priority ASC, id ASC
                """
            ).fetchall()

        candidate = text.strip()
        for row in rows:
            trigger_type = str(row["trigger_type"])
            trigger_text = str(row["trigger_text"])
            matched = False
            if trigger_type == "exact":
                matched = candidate == trigger_text
            elif trigger_type == "contains":
                matched = trigger_text in candidate
            elif trigger_type == "prefix":
                matched = candidate.startswith(trigger_text)
            elif trigger_type == "regex":
                try:
                    matched = re.search(trigger_text, candidate) is not None
                except re.error:
                    matched = False
            if matched:
                return row
        return None

    def record_audit_event(
        self,
        event_type: str,
        outcome: str,
        user_id: Optional[int] = None,
        admin_chat_id: Optional[int] = None,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        mapped_message_id: Optional[int] = None,
        msg_kind: Optional[str] = None,
        is_edited: bool = False,
        direction: Optional[str] = None,
        error_class: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        with self.lock:
            self.conn.execute(
                """
                INSERT INTO audit_events (
                    event_type, user_id, admin_chat_id, chat_id, message_id, mapped_message_id,
                    message_kind, is_edited, direction, outcome, error_class, error_code, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    user_id,
                    admin_chat_id,
                    chat_id,
                    message_id,
                    mapped_message_id,
                    msg_kind,
                    1 if is_edited else 0,
                    direction,
                    outcome,
                    error_class,
                    error_code,
                    utc_now_iso(),
                ),
            )
            self.conn.commit()

    def get_stats_counts(self, since_iso: str):
        with self.lock:
            rows = self.conn.execute(
                """
                SELECT event_type, outcome, COUNT(*) AS cnt
                FROM audit_events
                WHERE created_at >= ?
                GROUP BY event_type, outcome
                """,
                (since_iso,),
            ).fetchall()
        return rows

    def get_top_users_by_events(self, since_iso: str, limit_count: int = 10):
        with self.lock:
            return self.conn.execute(
                """
                SELECT user_id, COUNT(*) AS cnt
                FROM audit_events
                WHERE created_at >= ?
                  AND user_id IS NOT NULL
                GROUP BY user_id
                ORDER BY cnt DESC
                LIMIT ?
                """,
                (since_iso, limit_count),
            ).fetchall()

    def delete_mappings_by_admin_message(self, admin_chat_id: int, admin_message_id: int) -> int:
        with self.lock:
            cur = self.conn.execute(
                """
                DELETE FROM message_map
                WHERE admin_chat_id = ? AND admin_message_id = ?
                """,
                (admin_chat_id, admin_message_id),
            )
            self.conn.commit()
            return cur.rowcount


def is_admin_user(update: Update) -> bool:
    if not update.effective_user:
        return False
    return update.effective_user.id in config.ADMIN_CHAT_IDS


def get_admin_chat_id(update: Update) -> Optional[int]:
    if not update.effective_chat:
        return None
    if not is_admin_user(update):
        return None
    admin_chat_id = update.effective_chat.id
    if admin_chat_id in config.ADMIN_CHAT_IDS:
        return admin_chat_id
    if config.RELAY_MODE == "group_topic" and config.ADMIN_GROUP_CHAT_ID is not None:
        if admin_chat_id == config.ADMIN_GROUP_CHAT_ID:
            return admin_chat_id
    return None


def is_admin_command_context(update: Update) -> bool:
    if not update.effective_chat:
        return False
    if not is_admin_user(update):
        return False

    chat_id = update.effective_chat.id
    if chat_id in config.ADMIN_CHAT_IDS:
        return True
    if config.RELAY_MODE == "group_topic" and config.ADMIN_GROUP_CHAT_ID is not None:
        return chat_id == config.ADMIN_GROUP_CHAT_ID
    return False


def get_db(context: ContextTypes.DEFAULT_TYPE) -> RelayDB:
    return context.application.bot_data["db"]


async def ensure_user_topic(
    context: ContextTypes.DEFAULT_TYPE,
    db: RelayDB,
    user_id: int,
    username: Optional[str],
    full_name: str,
) -> Optional[int]:
    if config.ADMIN_GROUP_CHAT_ID is None:
        return None

    expected_title = build_user_topic_title(username, full_name, user_id)
    topic_row = db.get_user_topic(user_id)

    if topic_row is None:
        created = await context.bot.create_forum_topic(
            chat_id=config.ADMIN_GROUP_CHAT_ID,
            name=expected_title,
        )
        thread_id = int(created.message_thread_id)
        db.upsert_user_topic(
            user_id=user_id,
            admin_group_chat_id=config.ADMIN_GROUP_CHAT_ID,
            topic_thread_id=thread_id,
            topic_title=expected_title,
        )
        return thread_id

    thread_id = int(topic_row["topic_thread_id"])
    current_title = str(topic_row["topic_title"])
    if current_title != expected_title:
        try:
            await context.bot.edit_forum_topic(
                chat_id=config.ADMIN_GROUP_CHAT_ID,
                message_thread_id=thread_id,
                name=expected_title,
            )
            db.update_user_topic_title(user_id, expected_title)
        except TelegramError:
            logging.exception("edit topic title failed for user %s", user_id)
    return thread_id


def resolve_target_user_from_group_topic(db: RelayDB, update: Update) -> Optional[int]:
    if config.RELAY_MODE != "group_topic" or config.ADMIN_GROUP_CHAT_ID is None:
        return None
    if not update.effective_chat or update.effective_chat.id != config.ADMIN_GROUP_CHAT_ID:
        return None
    msg = update.message
    if not msg or msg.message_thread_id is None:
        return None
    return db.get_user_id_by_topic(config.ADMIN_GROUP_CHAT_ID, int(msg.message_thread_id))


def resolve_target_user_from_arg_or_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    db: RelayDB,
) -> Optional[int]:
    if context.args:
        try:
            return int(context.args[0])
        except ValueError:
            pass
    admin_chat_id = get_admin_chat_id(update)
    if admin_chat_id and update.message and update.message.reply_to_message:
        return db.get_target_user_by_admin_message(
            admin_chat_id, update.message.reply_to_message.message_id
        )
    return None


def parse_ban_extra_args(args: List[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not args:
        return None, None, None

    remaining = list(args)
    expires_at = parse_expiry_token(remaining[0])
    if expires_at is not None:
        remaining = remaining[1:]

    text = " ".join(remaining).strip()
    if not text:
        return expires_at, None, None

    if "|" in text:
        reason_text, note_text = text.split("|", 1)
        reason = reason_text.strip() or None
        note = note_text.strip() or None
    else:
        reason = text
        note = None

    return expires_at, reason, note


def parse_rule_add_payload(raw: str) -> Optional[Tuple[str, str, str]]:
    if "=>" not in raw:
        return None
    left, reply_text = raw.split("=>", 1)
    left = left.strip()
    reply_text = reply_text.strip()
    if not left or not reply_text:
        return None

    if " " not in left:
        return None
    trigger_type, trigger_text = left.split(" ", 1)
    trigger_type = trigger_type.strip().lower()
    trigger_text = trigger_text.strip()
    if trigger_type not in {"exact", "contains", "prefix", "regex"}:
        return None
    if not trigger_text:
        return None
    return trigger_type, trigger_text, reply_text


def parse_stats_window(arg: Optional[str]) -> Tuple[str, datetime]:
    text = (arg or "24h").strip().lower()
    now = datetime.now(timezone.utc)
    if text == "7d":
        return "7d", now - timedelta(days=7)
    if text == "30d":
        return "30d", now - timedelta(days=30)
    return "24h", now - timedelta(hours=24)


def session_panel_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows:
        user_id = int(row["user_id"])
        name = str(row["full_name"])
        display = name if len(name) <= 12 else f"{name[:11]}…"
        buttons.append([InlineKeyboardButton(f"{display} ({user_id})", callback_data=f"sess:{user_id}")])

    buttons.append([InlineKeyboardButton("清空会话", callback_data="sessclear")])
    return InlineKeyboardMarkup(buttons)


def ban_duration_keyboard(user_id: int, admin_message_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("1小时", callback_data=f"banfor:{user_id}:1h:{admin_message_id}"),
            InlineKeyboardButton("1天", callback_data=f"banfor:{user_id}:1d:{admin_message_id}"),
            InlineKeyboardButton("7天", callback_data=f"banfor:{user_id}:7d:{admin_message_id}"),
        ],
        [
            InlineKeyboardButton("30天", callback_data=f"banfor:{user_id}:30d:{admin_message_id}"),
            InlineKeyboardButton("永久", callback_data=f"banfor:{user_id}:permanent:{admin_message_id}"),
        ],
        [InlineKeyboardButton("返回", callback_data=f"actmenu:{user_id}:{admin_message_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def admin_action_keyboard(user_id: int, admin_message_id: Optional[int] = None) -> InlineKeyboardMarkup:
    ban_callback = f"banmenu:{user_id}:{admin_message_id}" if admin_message_id is not None else f"ban:{user_id}"
    rows = [
        [
            InlineKeyboardButton("封禁用户", callback_data=ban_callback),
            InlineKeyboardButton("解封用户", callback_data=f"unban:{user_id}"),
            InlineKeyboardButton("设为会话", callback_data=f"sess:{user_id}"),
        ],
        [
            InlineKeyboardButton("清空会话", callback_data="sessclear"),
            InlineKeyboardButton("用户ID", callback_data=f"uid:{user_id}"),
        ],
    ]
    if admin_message_id is not None:
        rows.append([InlineKeyboardButton("删除消息", callback_data=f"delpair:{admin_message_id}")])
    return InlineKeyboardMarkup(rows)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    db = get_db(context)
    user = update.effective_user
    if not is_admin_user(update):
        db.touch_user(user.id, user.username, user.full_name)
    await update.message.reply_text(config.START_MESSAGE or "已连接中继机器人。发送 /id 查看你的 Telegram 用户 ID。")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    await update.message.reply_text(f"你的 Telegram 用户 ID：{update.effective_user.id}")


async def chatid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat:
        return

    _ = context

    lines = [f"当前 Chat ID：{update.effective_chat.id}"]
    if update.message.message_thread_id is not None:
        lines.append(f"当前话题 Thread ID：{update.message.message_thread_id}")
    if config.ADMIN_GROUP_CHAT_ID is not None:
        lines.append(f"配置 ADMIN_GROUP_CHAT_ID：{config.ADMIN_GROUP_CHAT_ID}")
    if config.ADMIN_GROUP_GENERAL_THREAD_ID is not None:
        lines.append(
            f"配置 ADMIN_GROUP_GENERAL_THREAD_ID：{config.ADMIN_GROUP_GENERAL_THREAD_ID}"
        )
    await update.message.reply_text("\n".join(lines))


async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(f"当前机器人版本：{config.BOT_VERSION}")


async def recent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return
    db = get_db(context)
    n = 10
    if context.args:
        try:
            n = max(1, min(100, int(context.args[0])))
        except ValueError:
            await update.message.reply_text("用法: /recent 10")
            return

    rows = db.get_recent_users(n)
    rows = [row for row in rows if int(row["user_id"]) not in config.ADMIN_CHAT_IDS]
    if not rows:
        await update.message.reply_text("暂无用户记录。")
        return

    lines = ["最近活跃用户："]
    for idx, row in enumerate(rows, start=1):
        uname = f"@{row['username']}" if row["username"] else "-"
        lines.append(
            f"{idx}. {row['full_name']} | ID: {row['user_id']} | 用户名: {uname} | 最后活跃: {row['last_active_at']}"
        )
    await update.message.reply_text("\n".join(lines))


async def session_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return
    if (
        config.RELAY_MODE == "group_topic"
        and update.effective_chat
        and config.ADMIN_GROUP_CHAT_ID is not None
        and update.effective_chat.id == config.ADMIN_GROUP_CHAT_ID
    ):
        await update.message.reply_text("群组话题模式下无需 /session，请直接在对应用户话题发送消息。")
        return
    admin_chat_id = get_admin_chat_id(update)
    if admin_chat_id is None:
        await update.message.reply_text("无权限。")
        return
    db = get_db(context)

    if not context.args:
        current = db.get_current_session(admin_chat_id)
        recent_rows = db.get_recent_users(8)
        recent_rows = [row for row in recent_rows if int(row["user_id"]) not in config.ADMIN_CHAT_IDS]
        if not recent_rows:
            if current:
                await update.message.reply_text(f"当前会话用户 ID: {current}")
            else:
                await update.message.reply_text("当前没有会话。用法：/session <用户ID> 或 /session clear")
            return

        title = f"当前会话用户 ID: {current}" if current else "当前没有会话，点击下方按钮可快速切换："
        await update.message.reply_text(
            title,
            reply_markup=session_panel_keyboard(recent_rows),
        )
        return

    arg = context.args[0].strip().lower()
    if arg == "clear":
        db.set_current_session(admin_chat_id, None)
        await update.message.reply_text("已清空当前会话。")
        return

    try:
        target_user_id = int(arg)
    except ValueError:
        await update.message.reply_text("用法：/session <用户ID> 或 /session clear")
        return

    if db.is_user_banned(target_user_id):
        await update.message.reply_text(f"用户 {target_user_id} 已封禁，不能设为当前会话。")
        return

    db.set_current_session(admin_chat_id, target_user_id)
    await update.message.reply_text(f"当前会话已切换到用户：{target_user_id}")


async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return
    admin_chat_id = get_admin_chat_id(update)
    if admin_chat_id is None:
        await update.message.reply_text("无权限。")
        return
    db = get_db(context)

    target_user_id = resolve_target_user_from_arg_or_reply(update, context, db)
    if not target_user_id:
        await update.message.reply_text(
            "用法：/ban <用户ID> [1h|1d|7d|YYYY-MM-DD] [原因]，或回复用户转发消息后 /ban\n"
            "备注可用分隔符：原因 | 备注"
        )
        return

    extra_args: List[str] = []
    if context.args:
        first_arg_is_user_id = False
        try:
            int(context.args[0])
            first_arg_is_user_id = True
        except ValueError:
            first_arg_is_user_id = False
        if first_arg_is_user_id:
            extra_args = context.args[1:]
        else:
            extra_args = context.args

    expires_at, reason, note = parse_ban_extra_args(extra_args)

    already_banned = db.is_user_banned(target_user_id)
    db.ban_user(
        target_user_id,
        operator_admin_id=admin_chat_id,
        reason=reason,
        note=note,
        expires_at=expires_at,
    )

    if not already_banned and db.get_current_session(admin_chat_id) == target_user_id:
        db.set_current_session(admin_chat_id, None)

    ban_row = db.get_ban(target_user_id)
    if ban_row:
        await update.message.reply_text(format_ban_info(ban_row))
    else:
        await update.message.reply_text(f"用户 {target_user_id} 已封禁。")


async def banlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return

    db = get_db(context)
    n = 20
    if context.args:
        try:
            n = max(1, min(100, int(context.args[0])))
        except ValueError:
            await update.message.reply_text("用法：/banlist [数量]")
            return

    rows = db.list_active_bans(n)
    if not rows:
        await update.message.reply_text("当前没有有效封禁。")
        return

    lines = ["当前封禁列表："]
    for idx, row in enumerate(rows, start=1):
        reason = (row["reason"] or "-").strip()
        note = (row["note"] or "-").strip()
        lines.append(
            f"{idx}. 用户ID: {row['user_id']} | 到期: {format_expiry_display(row['expires_at'])} | 原因: {reason} | 备注: {note}"
        )
    await update.message.reply_text("\n".join(lines))


async def baninfo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return

    db = get_db(context)

    if context.args:
        try:
            target_user_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("用法：/baninfo <用户ID>，或回复用户转发消息后 /baninfo")
            return
    elif update.message.reply_to_message:
        admin_chat_id = get_admin_chat_id(update)
        if admin_chat_id is None:
            await update.message.reply_text("无权限。")
            return
        target_user_id = db.get_target_user_by_admin_message(
            admin_chat_id, update.message.reply_to_message.message_id
        )
        if not target_user_id:
            await update.message.reply_text("找不到这条消息对应的用户映射。")
            return
    else:
        await update.message.reply_text("用法：/baninfo <用户ID>，或回复用户转发消息后 /baninfo")
        return

    row = db.get_ban(target_user_id)
    if not row:
        await update.message.reply_text(f"用户 {target_user_id} 当前不在有效封禁列表。")
        return

    await update.message.reply_text(format_ban_info(row))


async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return
    db = get_db(context)
    target_user_id = resolve_target_user_from_arg_or_reply(update, context, db)
    if not target_user_id:
        await update.message.reply_text("用法：/unban <用户ID>，或回复用户转发消息后发送 /unban")
        return

    removed = db.unban_user(target_user_id)
    await update.message.reply_text(
        f"用户 {target_user_id} 已解封。" if removed else f"用户 {target_user_id} 当前不在封禁列表。"
    )


async def admin_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if not is_admin_user(update):
        await query.answer("无权限", show_alert=True)
        return
    admin_chat_id = get_admin_chat_id(update)
    if admin_chat_id is None:
        await query.answer("无权限", show_alert=True)
        return

    db = get_db(context)
    if query.data == "sessclear":
        db.set_current_session(admin_chat_id, None)
        await query.answer("已清空当前会话")
        return

    parts = query.data.split(":")
    action = parts[0]

    if action == "banmenu":
        if len(parts) != 3:
            await query.answer("无效操作")
            return
        try:
            user_id = int(parts[1])
            admin_message_id = int(parts[2])
        except ValueError:
            await query.answer("无效操作")
            return
        await query.edit_message_reply_markup(reply_markup=ban_duration_keyboard(user_id, admin_message_id))
        await query.answer("请选择封禁时长")
        return

    if action == "actmenu":
        if len(parts) != 3:
            await query.answer("无效操作")
            return
        try:
            user_id = int(parts[1])
            admin_message_id = int(parts[2])
        except ValueError:
            await query.answer("无效操作")
            return
        await query.edit_message_reply_markup(reply_markup=admin_action_keyboard(user_id, admin_message_id))
        await query.answer("已返回快捷操作")
        return

    if action == "banfor":
        if len(parts) != 4:
            await query.answer("无效操作")
            return
        try:
            target_id = int(parts[1])
            duration = parts[2]
            admin_message_id = int(parts[3])
        except ValueError:
            await query.answer("无效操作")
            return

        expires_at = None if duration == "permanent" else parse_expiry_token(duration)
        if duration != "permanent" and expires_at is None:
            await query.answer("无效封禁时长")
            return

        already_banned = db.is_user_banned(target_id)
        db.ban_user(target_id, operator_admin_id=admin_chat_id, expires_at=expires_at)
        if db.get_current_session(admin_chat_id) == target_id:
            db.set_current_session(admin_chat_id, None)

        await query.edit_message_reply_markup(reply_markup=admin_action_keyboard(target_id, admin_message_id))
        duration_text = format_unban_time_display(expires_at)
        status_text = "已封禁该用户" if not already_banned else "该用户已更新封禁"
        await query.answer(f"{status_text}（{duration_text}）")
        return

    try:
        if len(parts) != 2:
            raise ValueError
        target_id = int(parts[1])
    except ValueError:
        await query.answer("无效操作")
        return

    if action == "sess":
        if db.is_user_banned(target_id):
            await query.answer("该用户已封禁，不能设为会话", show_alert=True)
            return
        db.set_current_session(admin_chat_id, target_id)
        await query.answer(f"当前会话已切换到 {target_id}")
        return

    if action == "ban":
        already_banned = db.is_user_banned(target_id)
        db.ban_user(target_id, operator_admin_id=admin_chat_id)
        if db.get_current_session(admin_chat_id) == target_id:
            db.set_current_session(admin_chat_id, None)
        await query.answer("已封禁该用户" if not already_banned else "该用户已更新封禁")
        return

    if action == "unban":
        removed = db.unban_user(target_id)
        await query.answer("已解封该用户" if removed else "该用户当前不在封禁列表")
        return

    if action == "uid":
        await query.answer(f"用户 ID: {target_id}", show_alert=True)
        return

    if action == "delpair":
        mappings = db.get_maps_by_admin_message(admin_chat_id, target_id)
        if not mappings:
            await query.answer("没有可删除的消息")
            return
        deleted = 0
        failed = 0
        for row in mappings:
            try:
                await context.bot.delete_message(admin_chat_id, row["admin_message_id"])
                deleted += 1
            except (BadRequest, Forbidden, TelegramError):
                failed += 1
            try:
                await context.bot.delete_message(row["user_chat_id"], row["user_message_id"])
                deleted += 1
            except (BadRequest, Forbidden, TelegramError):
                failed += 1
        db.delete_mappings_by_admin_message(admin_chat_id, target_id)
        db.record_audit_event(
            event_type="delete_pair",
            outcome="success" if failed == 0 else "failed",
            admin_chat_id=admin_chat_id,
            chat_id=admin_chat_id,
            message_id=target_id,
            direction="admin_to_user",
            error_code=f"failed={failed}",
        )
        await query.answer(f"删除完成 成功{deleted} 失败{failed}")
        return

    await query.answer("未知操作")


async def rule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return

    db = get_db(context)
    if not context.args:
        await update.message.reply_text(
            "用法：\n"
            "/rule list\n"
            "/rule add <exact|contains|prefix|regex> <触发词> => <回复内容>\n"
            "/rule on <id> | /rule off <id> | /rule del <id>\n"
            "/rule test <文本>"
        )
        return

    sub = context.args[0].lower()

    if sub == "list":
        rows = db.list_auto_reply_rules(50)
        if not rows:
            await update.message.reply_text("当前没有自动回复规则。")
            return
        lines = ["自动回复规则："]
        for row in rows:
            lines.append(
                f"#{row['id']} [{row['trigger_type']}] 触发: {row['trigger_text']} | 回复: {row['reply_text']} | 优先级: {row['priority']} | 启用: {'是' if row['is_enabled'] else '否'}"
            )
        await update.message.reply_text("\n".join(lines))
        return

    if sub == "add":
        payload = " ".join(context.args[1:]).strip()
        parsed = parse_rule_add_payload(payload)
        if not parsed:
            await update.message.reply_text(
                "格式错误。用法：/rule add <exact|contains|prefix|regex> <触发词> => <回复内容>"
            )
            return
        trigger_type, trigger_text, reply_text = parsed
        if trigger_type == "regex":
            try:
                re.compile(trigger_text)
            except re.error:
                await update.message.reply_text("regex 规则无效，请检查正则表达式。")
                return

        rule_id = db.add_auto_reply_rule(
            trigger_type=trigger_type,
            trigger_text=trigger_text,
            reply_text=reply_text,
            priority=100,
            created_by_admin_id=int(update.effective_user.id),
        )
        await update.message.reply_text(f"规则已创建，ID: {rule_id}")
        return

    if sub in {"on", "off", "del"}:
        if len(context.args) < 2:
            await update.message.reply_text(f"用法：/rule {sub} <id>")
            return
        try:
            rule_id = int(context.args[1])
        except ValueError:
            await update.message.reply_text(f"用法：/rule {sub} <id>")
            return

        if sub == "del":
            ok = db.delete_auto_reply_rule(rule_id)
            await update.message.reply_text("已删除规则。" if ok else "规则不存在。")
            return

        ok = db.set_auto_reply_rule_enabled(rule_id, enabled=(sub == "on"))
        if not ok:
            await update.message.reply_text("规则不存在。")
            return
        await update.message.reply_text("规则已启用。" if sub == "on" else "规则已停用。")
        return

    if sub == "test":
        sample = " ".join(context.args[1:]).strip()
        if not sample:
            await update.message.reply_text("用法：/rule test <文本>")
            return
        row = db.match_auto_reply_rule(sample)
        if not row:
            await update.message.reply_text("未命中任何规则。")
            return
        await update.message.reply_text(
            f"命中规则 #{row['id']} [{row['trigger_type']}]\n触发: {row['trigger_text']}\n回复: {row['reply_text']}"
        )
        return

    await update.message.reply_text("未知子命令。可用：list/add/on/off/del/test")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return

    db = get_db(context)
    arg = context.args[0] if context.args else None
    window_label, since_dt = parse_stats_window(arg)
    since_iso = since_dt.replace(microsecond=0).isoformat()

    rows = db.get_stats_counts(since_iso)
    top_users = db.get_top_users_by_events(since_iso, 10)

    lines = [f"统计窗口：{window_label}"]
    if not rows:
        lines.append("暂无统计事件。")
    else:
        lines.append("事件统计：")
        for row in rows:
            lines.append(f"- {row['event_type']} / {row['outcome']} : {row['cnt']}")

    if top_users:
        lines.append("活跃用户TOP：")
        for idx, row in enumerate(top_users, start=1):
            lines.append(f"{idx}. 用户ID {row['user_id']} - 事件数 {row['cnt']}")

    await update.message.reply_text("\n".join(lines))


async def sender_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return
    admin_chat_id = get_admin_chat_id(update)
    if admin_chat_id is None:
        await update.message.reply_text("无权限。")
        return
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("请回复一条转发消息后再执行 /sender")
        return

    db = get_db(context)
    user_id = db.get_target_user_by_admin_message(admin_chat_id, reply.message_id)
    if not user_id:
        await update.message.reply_text("找不到这条消息对应的用户映射。")
        return
    await update.message.reply_text(f"该消息对应的用户 ID：{user_id}")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return
    admin_chat_id = get_admin_chat_id(update)
    if admin_chat_id is None:
        await update.message.reply_text("无权限。")
        return
    reply = update.message.reply_to_message
    inline_text = " ".join(context.args).strip() if context.args else ""
    if not reply and not inline_text:
        await update.message.reply_text("用法：回复消息后发送 /broadcast，或直接 /broadcast 你好")
        return

    db = get_db(context)
    users = db.get_all_users(exclude_user_id=admin_chat_id)
    users = [row for row in users if int(row["user_id"]) not in config.ADMIN_CHAT_IDS]
    if not users:
        await update.message.reply_text("没有可广播的用户。")
        return

    sent = 0
    failed = 0
    source_admin_message_id = reply.message_id if reply else update.message.message_id

    for row in users:
        target_id = int(row["user_id"])
        try:
            if reply:
                copied = await context.bot.copy_message(
                    chat_id=target_id,
                    from_chat_id=admin_chat_id,
                    message_id=reply.message_id,
                )
            else:
                copied = await context.bot.send_message(chat_id=target_id, text=inline_text)
            db.save_mapping(
                user_chat_id=target_id,
                admin_chat_id=admin_chat_id,
                user_message_id=copied.message_id,
                admin_message_id=source_admin_message_id,
                direction="broadcast",
            )
            db.record_audit_event(
                event_type="broadcast_out",
                outcome="success",
                user_id=target_id,
                admin_chat_id=admin_chat_id,
                chat_id=admin_chat_id,
                message_id=source_admin_message_id,
                mapped_message_id=copied.message_id,
                msg_kind=message_kind(reply) if reply else "text",
                direction="admin_to_user",
            )
            sent += 1
        except (Forbidden, BadRequest, TelegramError) as e:
            db.record_audit_event(
                event_type="broadcast_out",
                outcome="failed",
                user_id=target_id,
                admin_chat_id=admin_chat_id,
                chat_id=admin_chat_id,
                message_id=source_admin_message_id,
                msg_kind=message_kind(reply) if reply else "text",
                direction="admin_to_user",
                error_class=type(e).__name__,
            )
            failed += 1
        await asyncio.sleep(config.BROADCAST_DELAY_SECONDS)

    await update.message.reply_text(f"广播完成。成功: {sent}，失败: {failed}")


async def delete_pair_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_command_context(update):
        await update.message.reply_text("无权限。")
        return
    admin_chat_id = get_admin_chat_id(update)
    if admin_chat_id is None:
        await update.message.reply_text("无权限。")
        return
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("请回复一条消息后执行 /deletepair")
        return

    db = get_db(context)
    mappings = db.get_maps_by_admin_message(admin_chat_id, reply.message_id)
    if not mappings:
        await update.message.reply_text("没有找到可删除的消息。")
        return

    deleted = 0
    failed = 0

    for row in mappings:
        try:
            await context.bot.delete_message(admin_chat_id, row["admin_message_id"])
            deleted += 1
        except (BadRequest, Forbidden, TelegramError):
            failed += 1
        try:
            await context.bot.delete_message(row["user_chat_id"], row["user_message_id"])
            deleted += 1
        except (BadRequest, Forbidden, TelegramError):
            failed += 1
    db.delete_mappings_by_admin_message(admin_chat_id, reply.message_id)
    db.record_audit_event(
        event_type="delete_pair",
        outcome="success" if failed == 0 else "failed",
        admin_chat_id=admin_chat_id,
        chat_id=admin_chat_id,
        message_id=reply.message_id,
        direction="admin_to_user",
        error_code=f"failed={failed}",
    )
    await update.message.reply_text(f"删除完成。成功: {deleted}，失败: {failed}")


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    db = get_db(context)
    user = update.effective_user

    if is_admin_user(update):
        await handle_admin_message(update, context)
        return

    db.touch_user(user.id, user.username, user.full_name)
    db.record_audit_event(
        event_type="user_msg_in",
        outcome="success",
        user_id=user.id,
        chat_id=update.effective_chat.id,
        message_id=msg.message_id,
        msg_kind=message_kind(msg),
        direction="user_to_bot",
    )

    if db.is_user_banned(user.id):
        db.record_audit_event(
            event_type="blocked_ban",
            outcome="blocked",
            user_id=user.id,
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            msg_kind=message_kind(msg),
        )
        ban_row = db.get_ban(user.id)
        unban_time = format_unban_time_display(ban_row["expires_at"]) if ban_row else "永久"
        await msg.reply_text(f"您已被管理员封禁，解封时间：{unban_time}")
        return

    if msg.text is not None:
        rule = db.match_auto_reply_rule(msg.text)
        if rule is not None:
            db.record_audit_event(
                event_type="auto_reply_hit",
                outcome="success",
                user_id=user.id,
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                msg_kind="text",
                error_code=f"rule_id={int(rule['id'])}",
            )
            await msg.reply_text(str(rule["reply_text"]))
            return

    if config.RELAY_MODE == "group_topic" and config.ADMIN_GROUP_CHAT_ID is not None:
        try:
            topic_thread_id = await ensure_user_topic(
                context,
                db,
                user.id,
                user.username,
                user.full_name,
            )
        except TelegramError as e:
            db.record_audit_event(
                event_type="forward_user_to_admin",
                outcome="failed",
                user_id=user.id,
                admin_chat_id=config.ADMIN_GROUP_CHAT_ID,
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                msg_kind=message_kind(msg),
                direction="user_to_admin",
                error_class=type(e).__name__,
            )
            logging.exception("ensure user topic failed for %s: %s", user.id, e)
            await msg.reply_text("消息转发失败，请稍后重试。")
            return

        if topic_thread_id is None:
            await msg.reply_text("消息转发失败，请稍后重试。")
            return

        try:
            forwarded = await context.bot.copy_message(
                chat_id=config.ADMIN_GROUP_CHAT_ID,
                from_chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                message_thread_id=topic_thread_id,
                reply_markup=admin_action_keyboard(user.id),
            )
        except (BadRequest, Forbidden, TelegramError) as e:
            db.record_audit_event(
                event_type="forward_user_to_admin",
                outcome="failed",
                user_id=user.id,
                admin_chat_id=config.ADMIN_GROUP_CHAT_ID,
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                msg_kind=message_kind(msg),
                direction="user_to_admin",
                error_class=type(e).__name__,
            )
            logging.exception("forward user->group topic failed for %s: %s", user.id, e)
            await msg.reply_text("消息转发失败，请稍后重试。")
            return

        db.save_mapping(
            user_chat_id=user.id,
            admin_chat_id=config.ADMIN_GROUP_CHAT_ID,
            user_message_id=msg.message_id,
            admin_message_id=forwarded.message_id,
            direction="user_to_admin",
        )
        db.record_audit_event(
            event_type="forward_user_to_admin",
            outcome="success",
            user_id=user.id,
            admin_chat_id=config.ADMIN_GROUP_CHAT_ID,
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            mapped_message_id=forwarded.message_id,
            msg_kind=message_kind(msg),
            direction="user_to_admin",
        )
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=config.ADMIN_GROUP_CHAT_ID,
                message_id=forwarded.message_id,
                reply_markup=admin_action_keyboard(user.id, forwarded.message_id),
            )
        except TelegramError:
            logging.exception("set admin action keyboard failed for group forwarded message %s", forwarded.message_id)
        return

    user_card = (
        "来自用户的新消息\n"
        f"转发自：{display_name(user.username, user.full_name)}\n"
        f"ID：{user.id}\n"
        "内容：见下方转发消息"
    )

    forwarded_count = 0
    for admin_chat_id in config.ADMIN_CHAT_IDS:
        try:
            user_card_msg = await context.bot.send_message(chat_id=admin_chat_id, text=user_card)
            forwarded = await context.bot.forward_message(
                chat_id=admin_chat_id,
                from_chat_id=update.effective_chat.id,
                message_id=msg.message_id,
            )
        except (BadRequest, Forbidden, TelegramError) as e:
            db.record_audit_event(
                event_type="forward_user_to_admin",
                outcome="failed",
                user_id=user.id,
                admin_chat_id=admin_chat_id,
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                msg_kind=message_kind(msg),
                direction="user_to_admin",
                error_class=type(e).__name__,
            )
            logging.exception("forward user->admin failed for %s: %s", admin_chat_id, e)
            continue

        db.save_mapping(
            user_chat_id=user.id,
            admin_chat_id=admin_chat_id,
            user_message_id=msg.message_id,
            admin_message_id=forwarded.message_id,
            direction="user_to_admin",
        )
        db.record_audit_event(
            event_type="forward_user_to_admin",
            outcome="success",
            user_id=user.id,
            admin_chat_id=admin_chat_id,
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            mapped_message_id=forwarded.message_id,
            msg_kind=message_kind(msg),
            direction="user_to_admin",
        )
        await context.bot.edit_message_reply_markup(
            chat_id=admin_chat_id,
            message_id=user_card_msg.message_id,
            reply_markup=admin_action_keyboard(user.id, forwarded.message_id),
        )
        forwarded_count += 1

    if forwarded_count == 0:
        await msg.reply_text("消息转发失败，请稍后重试。")


async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return
    admin_chat_id = get_admin_chat_id(update)
    if admin_chat_id is None:
        return
    db = get_db(context)

    target_user_id = None
    if msg.reply_to_message:
        target_user_id = db.get_target_user_by_admin_message(
            admin_chat_id, msg.reply_to_message.message_id
        )
    if not target_user_id:
        target_user_id = resolve_target_user_from_group_topic(db, update)
    if not target_user_id:
        target_user_id = db.get_current_session(admin_chat_id)

    if not target_user_id:
        if config.RELAY_MODE == "group_topic" and config.ADMIN_GROUP_CHAT_ID is not None:
            if update.effective_chat and update.effective_chat.id == config.ADMIN_GROUP_CHAT_ID:
                await msg.reply_text("请在对应用户话题内发送消息，或回复一条用户映射消息。")
                return
        await msg.reply_text("请回复一条用户转发消息，或先用 /session <用户ID> 设定当前会话。")
        return
    if db.is_user_banned(target_user_id):
        await msg.reply_text(f"用户 {target_user_id} 已封禁，消息未发送。")
        return

    try:
        copied = await context.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=admin_chat_id,
            message_id=msg.message_id,
        )
    except (BadRequest, Forbidden, TelegramError) as e:
        db.record_audit_event(
            event_type="forward_admin_to_user",
            outcome="failed",
            user_id=target_user_id,
            admin_chat_id=admin_chat_id,
            chat_id=admin_chat_id,
            message_id=msg.message_id,
            msg_kind=message_kind(msg),
            direction="admin_to_user",
            error_class=type(e).__name__,
        )
        await msg.reply_text(f"发送失败，用户可能已屏蔽机器人。用户 ID：{target_user_id}")
        return

    db.save_mapping(
        user_chat_id=target_user_id,
        admin_chat_id=admin_chat_id,
        user_message_id=copied.message_id,
        admin_message_id=msg.message_id,
        direction="admin_to_user",
    )
    db.record_audit_event(
        event_type="forward_admin_to_user",
        outcome="success",
        user_id=target_user_id,
        admin_chat_id=admin_chat_id,
        chat_id=admin_chat_id,
        message_id=msg.message_id,
        mapped_message_id=copied.message_id,
        msg_kind=message_kind(msg),
        direction="admin_to_user",
    )


async def handle_edited_group_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_message
    if not msg or not update.effective_chat:
        return
    if config.RELAY_MODE != "group_topic" or config.ADMIN_GROUP_CHAT_ID is None:
        return
    if update.effective_chat.id != config.ADMIN_GROUP_CHAT_ID:
        return
    if not is_admin_user(update):
        return

    db = get_db(context)
    rows = db.get_admin_to_user_maps(config.ADMIN_GROUP_CHAT_ID, msg.message_id)
    for row in rows:
        try:
            if msg.text is not None:
                await context.bot.edit_message_text(
                    chat_id=row["user_chat_id"],
                    message_id=row["user_message_id"],
                    text=msg.text,
                    entities=msg.entities,
                )
            elif msg.caption is not None:
                await context.bot.edit_message_caption(
                    chat_id=row["user_chat_id"],
                    message_id=row["user_message_id"],
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                )
            db.record_audit_event(
                event_type="edit_sync_admin_to_user",
                outcome="success",
                user_id=int(row["user_chat_id"]),
                admin_chat_id=config.ADMIN_GROUP_CHAT_ID,
                chat_id=config.ADMIN_GROUP_CHAT_ID,
                message_id=msg.message_id,
                mapped_message_id=int(row["user_message_id"]),
                msg_kind=message_kind(msg),
                is_edited=True,
                direction="admin_to_user",
            )
        except (BadRequest, Forbidden, TelegramError) as e:
            db.record_audit_event(
                event_type="edit_sync_admin_to_user",
                outcome="failed",
                user_id=int(row["user_chat_id"]),
                admin_chat_id=config.ADMIN_GROUP_CHAT_ID,
                chat_id=config.ADMIN_GROUP_CHAT_ID,
                message_id=msg.message_id,
                mapped_message_id=int(row["user_message_id"]),
                msg_kind=message_kind(msg),
                is_edited=True,
                direction="admin_to_user",
                error_class=type(e).__name__,
            )
            continue


async def handle_edited_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_message
    if not msg or not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    db = get_db(context)

    if is_admin_user(update):
        admin_chat_id = get_admin_chat_id(update)
        if admin_chat_id is None:
            return
        rows = db.get_admin_to_user_maps(admin_chat_id, msg.message_id)
        for row in rows:
            try:
                if msg.text is not None:
                    await context.bot.edit_message_text(
                        chat_id=row["user_chat_id"],
                        message_id=row["user_message_id"],
                        text=msg.text,
                        entities=msg.entities,
                    )
                elif msg.caption is not None:
                    await context.bot.edit_message_caption(
                        chat_id=row["user_chat_id"],
                        message_id=row["user_message_id"],
                        caption=msg.caption,
                        caption_entities=msg.caption_entities,
                    )
                db.record_audit_event(
                    event_type="edit_sync_admin_to_user",
                    outcome="success",
                    user_id=int(row["user_chat_id"]),
                    admin_chat_id=admin_chat_id,
                    chat_id=admin_chat_id,
                    message_id=msg.message_id,
                    mapped_message_id=int(row["user_message_id"]),
                    msg_kind=message_kind(msg),
                    is_edited=True,
                    direction="admin_to_user",
                )
            except (BadRequest, Forbidden, TelegramError) as e:
                db.record_audit_event(
                    event_type="edit_sync_admin_to_user",
                    outcome="failed",
                    user_id=int(row["user_chat_id"]),
                    admin_chat_id=admin_chat_id,
                    chat_id=admin_chat_id,
                    message_id=msg.message_id,
                    mapped_message_id=int(row["user_message_id"]),
                    msg_kind=message_kind(msg),
                    is_edited=True,
                    direction="admin_to_user",
                    error_class=type(e).__name__,
                )
                continue
        return

    rows = db.get_user_to_admin_maps(update.effective_user.id, msg.message_id)
    if not rows:
        return

    for row in rows:
        try:
            if msg.text is not None:
                await context.bot.edit_message_text(
                    chat_id=row["admin_chat_id"],
                    message_id=row["admin_message_id"],
                    text=msg.text,
                    entities=msg.entities,
                )
            elif msg.caption is not None:
                await context.bot.edit_message_caption(
                    chat_id=row["admin_chat_id"],
                    message_id=row["admin_message_id"],
                    caption=msg.caption,
                    caption_entities=msg.caption_entities,
                )
            db.record_audit_event(
                event_type="edit_sync_user_to_admin",
                outcome="success",
                user_id=update.effective_user.id,
                admin_chat_id=int(row["admin_chat_id"]),
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                mapped_message_id=int(row["admin_message_id"]),
                msg_kind=message_kind(msg),
                is_edited=True,
                direction="user_to_admin",
            )
        except (BadRequest, Forbidden, TelegramError) as e:
            db.record_audit_event(
                event_type="edit_sync_user_to_admin",
                outcome="failed",
                user_id=update.effective_user.id,
                admin_chat_id=int(row["admin_chat_id"]),
                chat_id=update.effective_chat.id,
                message_id=msg.message_id,
                mapped_message_id=int(row["admin_message_id"]),
                msg_kind=message_kind(msg),
                is_edited=True,
                direction="user_to_admin",
                error_class=type(e).__name__,
            )
            continue


async def private_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat or not update.message:
        return
    if update.effective_chat.type == ChatType.PRIVATE:
        return

    if (
        config.RELAY_MODE == "group_topic"
        and config.ADMIN_GROUP_CHAT_ID is not None
        and update.effective_chat.id == config.ADMIN_GROUP_CHAT_ID
    ):
        if is_admin_user(update):
            await handle_admin_message(update, context)
        return

    await update.message.reply_text("该机器人仅支持私聊使用。")


def validate_config() -> None:
    if not config.BOT_TOKEN or "PLEASE_REPLACE" in config.BOT_TOKEN:
        raise RuntimeError("请先在 config.py 中填写 BOT_TOKEN。")
    if not config.ADMIN_CHAT_IDS:
        raise RuntimeError("请先在 config.py 中填写正确的 ADMIN_CHAT_ID。")
    if config.RELAY_MODE == "group_topic":
        if config.ADMIN_GROUP_CHAT_ID is None:
            raise RuntimeError("group_topic 模式下必须配置 ADMIN_GROUP_CHAT_ID。")


async def sync_if_changed(
    label: str,
    target_value: str,
    getter,
    updater,
) -> None:
    normalized_target = (target_value or "").strip()
    if not normalized_target:
        logging.info("%s 未配置，跳过同步。", label)
        return
    try:
        current_value = await getter()
        normalized_current = (current_value or "").strip()
        if normalized_current == normalized_target:
            logging.info("%s 无变更，跳过同步。", label)
            return
        await updater(normalized_target)
        logging.info("%s 已同步。", label)
    except RetryAfter as e:
        logging.warning("%s 触发频控，约 %s 秒后重试。", label, e.retry_after)
    except TelegramError:
        logging.exception("同步%s失败。", label)


async def setup_bot_profile(app: Application) -> None:
    if config.RELAY_MODE == "group_topic" and config.ADMIN_GROUP_CHAT_ID is not None:
        try:
            chat = await app.bot.get_chat(config.ADMIN_GROUP_CHAT_ID)
            if getattr(chat, "type", None) != ChatType.SUPERGROUP or not getattr(chat, "is_forum", False):
                logging.warning(
                    "group_topic 模式下 ADMIN_GROUP_CHAT_ID=%s 不是已开启话题的超级群，相关转发功能可能不可用。",
                    config.ADMIN_GROUP_CHAT_ID,
                )
        except TelegramError as e:
            logging.warning(
                "无法获取管理员群信息（ADMIN_GROUP_CHAT_ID=%s）：%s；程序继续运行，可在任意群执行 /chatid 排查。",
                config.ADMIN_GROUP_CHAT_ID,
                e,
            )

    async def get_description_value() -> str:
        data = await app.bot.get_my_description()
        return data.description or ""

    async def get_short_description_value() -> str:
        data = await app.bot.get_my_short_description()
        return data.short_description or ""

    async def get_name_value() -> str:
        data = await app.bot.get_my_name()
        return data.name or ""

    if config.BOT_USER_COMMANDS:
        try:
            user_commands = [BotCommand(command=c, description=d) for c, d in config.BOT_USER_COMMANDS]
            scope = BotCommandScopeAllPrivateChats()
            current = await app.bot.get_my_commands(scope=scope)
            current_pairs = [(c.command, c.description) for c in current]
            target_pairs = [(c.command, c.description) for c in user_commands]
            if current_pairs != target_pairs:
                await app.bot.set_my_commands(commands=user_commands, scope=scope)
                logging.info("普通用户命令菜单已同步。")
            else:
                logging.info("普通用户命令菜单无变更，跳过同步。")
        except RetryAfter as e:
            logging.warning("普通用户命令菜单触发频控，约 %s 秒后重试。", e.retry_after)
        except TelegramError:
            logging.exception("同步普通用户命令菜单失败。")

    if config.BOT_ADMIN_COMMANDS:
        try:
            admin_commands = [BotCommand(command=c, description=d) for c, d in config.BOT_ADMIN_COMMANDS]
            for admin_chat_id in config.ADMIN_CHAT_IDS:
                scope = BotCommandScopeChat(chat_id=admin_chat_id)
                current = await app.bot.get_my_commands(scope=scope)
                current_pairs = [(c.command, c.description) for c in current]
                target_pairs = [(c.command, c.description) for c in admin_commands]
                if current_pairs != target_pairs:
                    await app.bot.set_my_commands(commands=admin_commands, scope=scope)
                    logging.info("管理员(%s)命令菜单已同步。", admin_chat_id)
                else:
                    logging.info("管理员(%s)命令菜单无变更，跳过同步。", admin_chat_id)

            if config.RELAY_MODE == "group_topic" and config.ADMIN_GROUP_CHAT_ID is not None:
                scope = BotCommandScopeChat(chat_id=config.ADMIN_GROUP_CHAT_ID)
                current = await app.bot.get_my_commands(scope=scope)
                current_pairs = [(c.command, c.description) for c in current]
                target_pairs = [(c.command, c.description) for c in admin_commands]
                if current_pairs != target_pairs:
                    await app.bot.set_my_commands(commands=admin_commands, scope=scope)
                    logging.info("管理员群命令菜单已同步。")
                else:
                    logging.info("管理员群命令菜单无变更，跳过同步。")
        except RetryAfter as e:
            logging.warning("管理员命令菜单触发频控，约 %s 秒后重试。", e.retry_after)
        except TelegramError:
            logging.exception("同步管理员命令菜单失败。")

    description = trim_with_log("BOT_DESCRIPTION", config.BOT_DESCRIPTION, MAX_BOT_DESCRIPTION_LEN)
    await sync_if_changed(
        "机器人简介",
        description,
        get_description_value,
        lambda v: app.bot.set_my_description(description=v),
    )

    short_description = trim_with_log(
        "BOT_SHORT_DESCRIPTION",
        config.BOT_SHORT_DESCRIPTION,
        MAX_BOT_SHORT_DESCRIPTION_LEN,
    )
    await sync_if_changed(
        "机器人短简介",
        short_description,
        get_short_description_value,
        lambda v: app.bot.set_my_short_description(short_description=v),
    )

    bot_name = trim_with_log("BOT_NAME", config.BOT_NAME, MAX_BOT_NAME_LEN)
    await sync_if_changed(
        "机器人名称",
        bot_name,
        get_name_value,
        lambda v: app.bot.set_my_name(name=v),
    )


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        level=logging.INFO,
    )
    validate_config()

    app = Application.builder().token(config.BOT_TOKEN).post_init(setup_bot_profile).build()
    app.bot_data["db"] = RelayDB(config.DB_PATH)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("chatid", chatid_cmd))
    app.add_handler(CommandHandler("version", version_cmd))
    app.add_handler(CommandHandler("recent", recent_cmd))
    app.add_handler(CommandHandler("session", session_cmd))
    app.add_handler(CommandHandler("ban", ban_cmd))
    app.add_handler(CommandHandler("banlist", banlist_cmd))
    app.add_handler(CommandHandler("baninfo", baninfo_cmd))
    app.add_handler(CommandHandler("unban", unban_cmd))
    app.add_handler(CommandHandler("rule", rule_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("sender", sender_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("deletepair", delete_pair_cmd))
    app.add_handler(
        CallbackQueryHandler(
            admin_action_callback,
            pattern=r"^(?:sessclear|(?:ban|unban|sess|uid|delpair):\d+|banmenu:\d+:\d+|actmenu:\d+:\d+|banfor:\d+:[^:]+:\d+)$",
        )
    )

    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            handle_private_message,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.UpdateType.EDITED_MESSAGE & filters.ChatType.PRIVATE,
            handle_edited_private_message,
        )
    )
    if config.RELAY_MODE == "group_topic" and config.ADMIN_GROUP_CHAT_ID is not None:
        app.add_handler(
            MessageHandler(
                filters.UpdateType.EDITED_MESSAGE
                & ~filters.ChatType.PRIVATE
                & filters.Chat(config.ADMIN_GROUP_CHAT_ID)
                & ~filters.COMMAND,
                handle_edited_group_admin_message,
            )
        )
    app.add_handler(
        MessageHandler(
            ~filters.ChatType.PRIVATE & ~filters.COMMAND,
            private_guard,
        )
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
