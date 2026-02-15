import os
from typing import List, Tuple

from dotenv import load_dotenv


load_dotenv()


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


def get_float_env(name: str, default: str) -> float:
    raw = os.getenv(name, default).strip()
    try:
        return float(raw)
    except ValueError as e:
        raise RuntimeError(f"环境变量 {name} 不是合法数字: {raw}") from e


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_CHAT_ID = get_int_env("ADMIN_CHAT_ID", "0")
DB_PATH = os.getenv("DB_PATH", "relay_bot.db").strip()
BROADCAST_DELAY_SECONDS = get_float_env("BROADCAST_DELAY_SECONDS", "0.05")
START_MESSAGE = os.getenv("START_MESSAGE", "").replace("\\n", "\n").strip()
BOT_NAME = os.getenv("BOT_NAME", "").strip()
BOT_DESCRIPTION = os.getenv("BOT_DESCRIPTION", "").replace("\\n", "\n").strip()
BOT_SHORT_DESCRIPTION = os.getenv("BOT_SHORT_DESCRIPTION", "").replace("\\n", "\n").strip()
BOT_USER_COMMANDS = parse_bot_commands(os.getenv("BOT_USER_COMMANDS", ""))
BOT_ADMIN_COMMANDS = parse_bot_commands(
    os.getenv("BOT_ADMIN_COMMANDS", os.getenv("BOT_COMMANDS", ""))
)
