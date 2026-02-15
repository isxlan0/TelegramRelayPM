import os
from typing import List, Optional, Tuple

from dotenv import load_dotenv


load_dotenv()


VALID_RELAY_MODES = {"private", "group_topic"}


def parse_bot_commands(raw: str) -> List[Tuple[str, str]]:
    commands: List[Tuple[str, str]] = []
    for item in raw.split(";"):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            continue
        command, description = item.split(":", 1)
        command = command.strip().lstrip("/")
        description = description.strip()
        if command and description:
            commands.append((command, description))
    return commands


def get_int_env(name: str, default: str) -> int:
    raw = os.getenv(name, default).strip()
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"环境变量 {name} 不是合法整数: {raw}") from e


def get_optional_int_env(name: str) -> Optional[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"环境变量 {name} 不是合法整数: {raw}") from e


def get_float_env(name: str, default: str) -> float:
    raw = os.getenv(name, default).strip()
    try:
        return float(raw)
    except ValueError as e:
        raise RuntimeError(f"环境变量 {name} 不是合法数字: {raw}") from e


def parse_admin_chat_ids(raw: str) -> List[int]:
    parts = [part.strip() for part in raw.split("|") if part.strip()]
    if not parts:
        raise RuntimeError("环境变量 ADMIN_CHAT_ID 不能为空")

    admin_ids: List[int] = []
    seen = set()
    for part in parts:
        try:
            admin_id = int(part)
        except ValueError as e:
            raise RuntimeError(f"环境变量 ADMIN_CHAT_ID 包含非法ID: {part}") from e
        if admin_id <= 0:
            raise RuntimeError(f"环境变量 ADMIN_CHAT_ID 包含非法ID: {part}")
        if admin_id not in seen:
            seen.add(admin_id)
            admin_ids.append(admin_id)
    return admin_ids


def parse_relay_mode(raw: str) -> str:
    value = (raw or "private").strip().lower()
    if value not in VALID_RELAY_MODES:
        raise RuntimeError(
            f"环境变量 RELAY_MODE 仅支持 {', '.join(sorted(VALID_RELAY_MODES))}，当前值: {value}"
        )
    return value


def parse_admin_group_chat_id(raw: str) -> Optional[int]:
    text = (raw or "").strip()
    if not text:
        return None

    candidate = text
    if "t.me/c/" in text:
        try:
            candidate = text.split("t.me/c/", 1)[1].split("/", 1)[0].strip()
        except (IndexError, ValueError):
            raise RuntimeError(f"环境变量 ADMIN_GROUP_CHAT_ID 格式不正确: {text}")

    if candidate.startswith("-100"):
        try:
            value = int(candidate)
        except ValueError as e:
            raise RuntimeError(f"环境变量 ADMIN_GROUP_CHAT_ID 不是合法群ID: {text}") from e
        if value >= 0:
            raise RuntimeError(f"环境变量 ADMIN_GROUP_CHAT_ID 不是合法群ID: {text}")
        return value

    if candidate.isdigit():
        short_id = int(candidate)
        return -(1_000_000_000_000 + short_id)

    raise RuntimeError(
        f"环境变量 ADMIN_GROUP_CHAT_ID 格式不正确: {text}。可填写 -100... 或 t.me/c/xxx/1 中的 xxx"
    )


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_IDS = parse_admin_chat_ids(os.getenv("ADMIN_CHAT_ID", "0"))
ADMIN_CHAT_ID = ADMIN_CHAT_IDS[0]
PRIMARY_ADMIN_CHAT_ID = ADMIN_CHAT_ID
RELAY_MODE = parse_relay_mode(os.getenv("RELAY_MODE", "private"))
ADMIN_GROUP_CHAT_ID = parse_admin_group_chat_id(os.getenv("ADMIN_GROUP_CHAT_ID", ""))
ADMIN_GROUP_GENERAL_THREAD_ID = get_optional_int_env("ADMIN_GROUP_GENERAL_THREAD_ID")
DB_PATH = os.getenv("DB_PATH", "relay_bot.db").strip()
BROADCAST_DELAY_SECONDS = get_float_env("BROADCAST_DELAY_SECONDS", "1.0")
START_MESSAGE = os.getenv("START_MESSAGE", "").replace("\\n", "\n").strip()
BOT_NAME = os.getenv("BOT_NAME", "").strip()
BOT_VERSION = os.getenv("BOT_VERSION", "v1.0.3").strip() or "v1.0.3"
BOT_DESCRIPTION = os.getenv("BOT_DESCRIPTION", "").replace("\\n", "\n").strip()
BOT_SHORT_DESCRIPTION = os.getenv("BOT_SHORT_DESCRIPTION", "").replace("\\n", "\n").strip()
BOT_USER_COMMANDS = parse_bot_commands(os.getenv("BOT_USER_COMMANDS", ""))
BOT_ADMIN_COMMANDS = parse_bot_commands(
    os.getenv("BOT_ADMIN_COMMANDS", os.getenv("BOT_COMMANDS", ""))
)
