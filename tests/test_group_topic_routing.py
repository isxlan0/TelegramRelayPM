import importlib
import logging
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace


def install_fake_telegram_modules() -> None:
    telegram = types.ModuleType("telegram")
    telegram.BotCommand = type("BotCommand", (), {"__init__": lambda self, *args, **kwargs: None})
    telegram.BotCommandScopeAllPrivateChats = type(
        "BotCommandScopeAllPrivateChats", (), {"__init__": lambda self, *args, **kwargs: None}
    )
    telegram.BotCommandScopeChat = type(
        "BotCommandScopeChat", (), {"__init__": lambda self, *args, **kwargs: None}
    )
    telegram.InlineKeyboardButton = type(
        "InlineKeyboardButton", (), {"__init__": lambda self, *args, **kwargs: None}
    )
    telegram.InlineKeyboardMarkup = type(
        "InlineKeyboardMarkup", (), {"__init__": lambda self, *args, **kwargs: None}
    )
    telegram.Update = type("Update", (), {})

    constants = types.ModuleType("telegram.constants")
    constants.ChatType = SimpleNamespace(PRIVATE="private", SUPERGROUP="supergroup")

    error = types.ModuleType("telegram.error")

    class TelegramError(Exception):
        pass

    class BadRequest(TelegramError):
        pass

    class Forbidden(TelegramError):
        pass

    class RetryAfter(TelegramError):
        def __init__(self, retry_after=1):
            super().__init__(retry_after)
            self.retry_after = retry_after

    error.TelegramError = TelegramError
    error.BadRequest = BadRequest
    error.Forbidden = Forbidden
    error.RetryAfter = RetryAfter

    ext = types.ModuleType("telegram.ext")
    ext.Application = type("Application", (), {})
    ext.CallbackQueryHandler = type("CallbackQueryHandler", (), {})
    ext.CommandHandler = type("CommandHandler", (), {})
    ext.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
    ext.MessageHandler = type("MessageHandler", (), {})
    ext.filters = SimpleNamespace()

    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda *args, **kwargs: None

    sys.modules["telegram"] = telegram
    sys.modules["telegram.constants"] = constants
    sys.modules["telegram.error"] = error
    sys.modules["telegram.ext"] = ext
    sys.modules["dotenv"] = dotenv


def import_bot():
    os.environ["ADMIN_CHAT_ID"] = "1"
    os.environ["RELAY_MODE"] = "group_topic"
    os.environ["ADMIN_GROUP_CHAT_ID"] = "-1001000"
    os.environ["ADMIN_GROUP_GENERAL_THREAD_ID"] = "1"
    os.environ["BOT_TOKEN"] = "test-token"
    install_fake_telegram_modules()
    sys.modules.pop("config", None)
    sys.modules.pop("bot", None)
    return importlib.import_module("bot")


class FakeDB:
    def __init__(self):
        self.reply_targets = {}
        self.topic_targets = {}
        self.banned = set()
        self.saved_mappings = []
        self.audit_events = []

    def get_target_user_by_admin_message(self, admin_chat_id, admin_message_id):
        return self.reply_targets.get((admin_chat_id, admin_message_id))

    def get_user_id_by_topic(self, admin_group_chat_id, topic_thread_id):
        return self.topic_targets.get((admin_group_chat_id, topic_thread_id))

    def is_user_banned(self, user_id):
        return user_id in self.banned

    def save_mapping(self, **kwargs):
        self.saved_mappings.append(kwargs)

    def record_audit_event(self, **kwargs):
        self.audit_events.append(kwargs)


class FakeBot:
    def __init__(self):
        self.copy_calls = []

    async def copy_message(self, **kwargs):
        self.copy_calls.append(kwargs)
        return SimpleNamespace(message_id=9001)


class FakeMessage:
    def __init__(self, message_id=10, thread_id=2, reply_to_message=None, text="hello"):
        self.message_id = message_id
        self.message_thread_id = thread_id
        self.reply_to_message = reply_to_message
        self.text = text
        self.photo = None
        self.video = None
        self.document = None
        self.audio = None
        self.voice = None
        self.sticker = None
        self.animation = None
        self.location = None
        self.contact = None
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class GroupTopicRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot_module = import_bot()
        self.bot_module.config.RELAY_MODE = "group_topic"
        self.bot_module.config.ADMIN_GROUP_CHAT_ID = -1001000
        self.bot_module.config.ADMIN_GROUP_GENERAL_THREAD_ID = 1
        self.bot_module.config.ADMIN_CHAT_IDS = [1]

    async def test_non_admin_message_in_bound_topic_forwards_to_user(self):
        db = FakeDB()
        db.topic_targets[(-1001000, 42)] = 555
        fake_bot = FakeBot()
        context = SimpleNamespace(bot=fake_bot, application=SimpleNamespace(bot_data={"db": db}))
        message = FakeMessage(message_id=123, thread_id=42)
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=-1001000, type="supergroup"),
            effective_user=SimpleNamespace(id=999),
        )

        await self.bot_module.handle_group_topic_message(update, context)

        self.assertEqual(fake_bot.copy_calls, [{"chat_id": 555, "from_chat_id": -1001000, "message_id": 123}])
        self.assertEqual(
            db.saved_mappings,
            [
                {
                    "user_chat_id": 555,
                    "admin_chat_id": -1001000,
                    "user_message_id": 9001,
                    "admin_message_id": 123,
                    "direction": "admin_to_user",
                }
            ],
        )
        self.assertEqual(db.audit_events[-1]["outcome"], "success")

    async def test_general_thread_is_not_forwarded(self):
        db = FakeDB()
        db.topic_targets[(-1001000, 1)] = 555
        fake_bot = FakeBot()
        context = SimpleNamespace(bot=fake_bot, application=SimpleNamespace(bot_data={"db": db}))
        message = FakeMessage(message_id=124, thread_id=1)
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=-1001000, type="supergroup"),
            effective_user=SimpleNamespace(id=999),
        )

        await self.bot_module.handle_group_topic_message(update, context)

        self.assertEqual(fake_bot.copy_calls, [])
        self.assertEqual(db.saved_mappings, [])
        self.assertEqual(db.audit_events[-1]["outcome"], "skipped")
        self.assertIn("没有绑定用户", message.replies[-1])

    async def test_reply_mapping_overrides_topic_target(self):
        db = FakeDB()
        db.topic_targets[(-1001000, 42)] = 555
        db.reply_targets[(-1001000, 77)] = 666
        fake_bot = FakeBot()
        context = SimpleNamespace(bot=fake_bot, application=SimpleNamespace(bot_data={"db": db}))
        reply = SimpleNamespace(message_id=77)
        message = FakeMessage(message_id=125, thread_id=42, reply_to_message=reply)
        update = SimpleNamespace(
            message=message,
            effective_chat=SimpleNamespace(id=-1001000, type="supergroup"),
            effective_user=SimpleNamespace(id=999),
        )

        await self.bot_module.handle_group_topic_message(update, context)

        self.assertEqual(fake_bot.copy_calls[0]["chat_id"], 666)
        self.assertEqual(db.saved_mappings[0]["user_chat_id"], 666)

    def test_configure_logging_writes_start_time_log_file(self):
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                log_path = self.bot_module.configure_logging()
                logging.shutdown()
                path = Path(log_path)
                self.assertRegex(path.name, r"^\d{8}_\d{6}\.log$")
                self.assertTrue(path.exists())
                self.assertIn("日志文件已创建", path.read_text(encoding="utf-8"))
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()
