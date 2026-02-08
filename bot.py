import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat, Update
from telegram.constants import ChatType
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
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


def trim_with_log(label: str, value: str, max_len: int) -> str:
    if len(value) <= max_len:
        return value
    logging.warning("%s 超出长度限制，已自动截断到 %d 字符。", label, max_len)
    return value[:max_len]


class RelayDB:
    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
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
            """
        )
        self.conn.commit()

    def touch_user(self, user_id: int, username: Optional[str], full_name: str) -> None:
        now = utc_now_iso()
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

    def get_admin_to_user_maps(self, admin_chat_id: int, admin_message_id: int):
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
        if exclude_user_id is None:
            return self.conn.execute(
                "SELECT user_id FROM users ORDER BY last_active_at DESC"
            ).fetchall()
        return self.conn.execute(
            "SELECT user_id FROM users WHERE user_id != ? ORDER BY last_active_at DESC",
            (exclude_user_id,),
        ).fetchall()

    def set_current_session(self, admin_chat_id: int, user_id: Optional[int]) -> None:
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

    def delete_mappings_by_admin_message(self, admin_chat_id: int, admin_message_id: int) -> int:
        cur = self.conn.execute(
            """
            DELETE FROM message_map
            WHERE admin_chat_id = ? AND admin_message_id = ?
            """,
            (admin_chat_id, admin_message_id),
        )
        self.conn.commit()
        return cur.rowcount


def is_admin_chat(update: Update) -> bool:
    if not update.effective_chat:
        return False
    return update.effective_chat.id == config.ADMIN_CHAT_ID


def get_db(context: ContextTypes.DEFAULT_TYPE) -> RelayDB:
    return context.application.bot_data["db"]


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    db = get_db(context)
    user = update.effective_user
    if user.id != config.ADMIN_CHAT_ID:
        db.touch_user(user.id, user.username, user.full_name)
    await update.message.reply_text(config.START_MESSAGE or "已连接中继机器人。发送 /id 查看你的 Telegram 用户 ID。")


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    await update.message.reply_text(f"你的 Telegram 用户 ID：`{update.effective_user.id}`", parse_mode="Markdown")


async def recent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_chat(update):
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

    rows = db.get_recent_users(n, exclude_user_id=config.ADMIN_CHAT_ID)
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
    if not is_admin_chat(update):
        await update.message.reply_text("无权限。")
        return
    db = get_db(context)

    if not context.args:
        current = db.get_current_session(config.ADMIN_CHAT_ID)
        if current:
            await update.message.reply_text(f"当前会话用户 ID: {current}")
        else:
            await update.message.reply_text("当前没有会话。用法：/session <用户ID> 或 /session clear")
        return

    arg = context.args[0].strip().lower()
    if arg == "clear":
        db.set_current_session(config.ADMIN_CHAT_ID, None)
        await update.message.reply_text("已清空当前会话。")
        return

    try:
        target_user_id = int(arg)
    except ValueError:
        await update.message.reply_text("用法：/session <用户ID> 或 /session clear")
        return

    db.set_current_session(config.ADMIN_CHAT_ID, target_user_id)
    await update.message.reply_text(f"当前会话已切换到用户：{target_user_id}")


async def sender_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_chat(update):
        await update.message.reply_text("无权限。")
        return
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("请回复一条转发消息后再执行 /sender")
        return

    db = get_db(context)
    user_id = db.get_target_user_by_admin_message(config.ADMIN_CHAT_ID, reply.message_id)
    if not user_id:
        await update.message.reply_text("找不到这条消息对应的用户映射。")
        return
    await update.message.reply_text(f"该消息对应的用户 ID：{user_id}")


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_chat(update):
        await update.message.reply_text("无权限。")
        return
    reply = update.message.reply_to_message
    inline_text = " ".join(context.args).strip() if context.args else ""
    if not reply and not inline_text:
        await update.message.reply_text("用法：回复消息后发送 /broadcast，或直接 /broadcast 你好")
        return

    db = get_db(context)
    users = db.get_all_users(exclude_user_id=config.ADMIN_CHAT_ID)
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
                    from_chat_id=config.ADMIN_CHAT_ID,
                    message_id=reply.message_id,
                )
            else:
                copied = await context.bot.send_message(chat_id=target_id, text=inline_text)
            db.save_mapping(
                user_chat_id=target_id,
                admin_chat_id=config.ADMIN_CHAT_ID,
                user_message_id=copied.message_id,
                admin_message_id=source_admin_message_id,
                direction="broadcast",
            )
            sent += 1
        except (Forbidden, BadRequest, TelegramError):
            failed += 1
        await asyncio.sleep(config.BROADCAST_DELAY_SECONDS)

    await update.message.reply_text(f"广播完成。成功: {sent}，失败: {failed}")


async def delete_pair_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_admin_chat(update):
        await update.message.reply_text("无权限。")
        return
    reply = update.message.reply_to_message
    if not reply:
        await update.message.reply_text("请回复一条映射消息后执行 /deletepair")
        return

    db = get_db(context)
    mappings = db.get_maps_by_admin_message(config.ADMIN_CHAT_ID, reply.message_id)
    if not mappings:
        await update.message.reply_text("没有找到可删除的映射。")
        return

    deleted = 0
    failed = 0

    for row in mappings:
        try:
            await context.bot.delete_message(config.ADMIN_CHAT_ID, row["admin_message_id"])
            deleted += 1
        except (BadRequest, Forbidden, TelegramError):
            failed += 1
        try:
            await context.bot.delete_message(row["user_chat_id"], row["user_message_id"])
            deleted += 1
        except (BadRequest, Forbidden, TelegramError):
            failed += 1

    db.delete_mappings_by_admin_message(config.ADMIN_CHAT_ID, reply.message_id)
    await update.message.reply_text(f"删除完成。成功: {deleted}，失败: {failed}")


async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg or not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    db = get_db(context)
    user = update.effective_user

    if user.id == config.ADMIN_CHAT_ID:
        await handle_admin_message(update, context)
        return

    db.touch_user(user.id, user.username, user.full_name)

    user_card = (
        "来自用户的新消息\n"
        f"转发自：{display_name(user.username, user.full_name)}\n"
        f"ID：{user.id}\n"
        "内容：见下方转发消息"
    )
    await context.bot.send_message(chat_id=config.ADMIN_CHAT_ID, text=user_card)

    try:
        forwarded = await context.bot.forward_message(
            chat_id=config.ADMIN_CHAT_ID,
            from_chat_id=update.effective_chat.id,
            message_id=msg.message_id,
        )
    except (BadRequest, Forbidden, TelegramError) as e:
        logging.exception("forward user->admin failed: %s", e)
        await msg.reply_text("消息转发失败，请稍后重试。")
        return

    db.save_mapping(
        user_chat_id=user.id,
        admin_chat_id=config.ADMIN_CHAT_ID,
        user_message_id=msg.message_id,
        admin_message_id=forwarded.message_id,
        direction="user_to_admin",
    )


async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg:
        return
    db = get_db(context)

    target_user_id = None
    if msg.reply_to_message:
        target_user_id = db.get_target_user_by_admin_message(
            config.ADMIN_CHAT_ID, msg.reply_to_message.message_id
        )
    if not target_user_id:
        target_user_id = db.get_current_session(config.ADMIN_CHAT_ID)

    if not target_user_id:
        await msg.reply_text("请回复一条用户转发消息，或先用 /session <用户ID> 设定当前会话。")
        return

    try:
        copied = await context.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=config.ADMIN_CHAT_ID,
            message_id=msg.message_id,
        )
    except (BadRequest, Forbidden, TelegramError):
        await msg.reply_text(f"发送失败，用户可能已屏蔽机器人。用户 ID：{target_user_id}")
        return

    db.save_mapping(
        user_chat_id=target_user_id,
        admin_chat_id=config.ADMIN_CHAT_ID,
        user_message_id=copied.message_id,
        admin_message_id=msg.message_id,
        direction="admin_to_user",
    )


async def handle_edited_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.edited_message
    if not msg or not update.effective_user or not update.effective_chat:
        return
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    db = get_db(context)
    user = update.effective_user

    if user.id == config.ADMIN_CHAT_ID:
        rows = db.get_admin_to_user_maps(config.ADMIN_CHAT_ID, msg.message_id)
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
            except (BadRequest, Forbidden, TelegramError):
                continue
        return

    row = db.get_user_to_admin_map(user.id, msg.message_id)
    if not row:
        return

    try:
        if msg.text is not None:
            await context.bot.edit_message_text(
                chat_id=config.ADMIN_CHAT_ID,
                message_id=row["admin_message_id"],
                text=msg.text,
                entities=msg.entities,
            )
        elif msg.caption is not None:
            await context.bot.edit_message_caption(
                chat_id=config.ADMIN_CHAT_ID,
                message_id=row["admin_message_id"],
                caption=msg.caption,
                caption_entities=msg.caption_entities,
            )
    except (BadRequest, Forbidden, TelegramError):
        return


async def private_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat and update.effective_chat.type != ChatType.PRIVATE and update.message:
        await update.message.reply_text("该机器人仅支持私聊使用。")


def validate_config() -> None:
    if not config.BOT_TOKEN or "PLEASE_REPLACE" in config.BOT_TOKEN:
        raise RuntimeError("请先在 config.py 中填写 BOT_TOKEN。")
    if not isinstance(config.ADMIN_CHAT_ID, int) or config.ADMIN_CHAT_ID == 0:
        raise RuntimeError("请先在 config.py 中填写正确的 ADMIN_CHAT_ID。")


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
            scope = BotCommandScopeChat(chat_id=config.ADMIN_CHAT_ID)
            current = await app.bot.get_my_commands(scope=scope)
            current_pairs = [(c.command, c.description) for c in current]
            target_pairs = [(c.command, c.description) for c in admin_commands]
            if current_pairs != target_pairs:
                await app.bot.set_my_commands(commands=admin_commands, scope=scope)
                logging.info("管理员命令菜单已同步。")
            else:
                logging.info("管理员命令菜单无变更，跳过同步。")
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
    app.add_handler(CommandHandler("recent", recent_cmd))
    app.add_handler(CommandHandler("session", session_cmd))
    app.add_handler(CommandHandler("sender", sender_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("deletepair", delete_pair_cmd))

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
    app.add_handler(
        MessageHandler(
            ~filters.ChatType.PRIVATE & ~filters.COMMAND,
            private_guard,
        )
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
