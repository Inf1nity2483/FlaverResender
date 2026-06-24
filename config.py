from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from links import LinkReplaceRules

load_dotenv()


def _require(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _int(name: str, default: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        if default is None:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return default
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return float(raw)


def _load_link_rules() -> LinkReplaceRules:
    from_csv = os.getenv("LINK_REPLACE_FROM", "").strip()
    prefixes = tuple(p.strip() for p in from_csv.split(",") if p.strip())
    if not prefixes:
        raise RuntimeError("LINK_REPLACE_FROM must contain at least one prefix")

    rules: list[tuple[tuple[str, ...], str]] = [(prefixes, _require("LINK_REPLACE_TO"))]

    from2 = os.getenv("LINK_REPLACE_FROM_2", "").strip()
    to2 = os.getenv("LINK_REPLACE_TO_2", "").strip()
    if from2 or to2:
        if not (from2 and to2):
            raise RuntimeError(
                "LINK_REPLACE_FROM_2 and LINK_REPLACE_TO_2 must both be set or both empty"
            )
        p2 = tuple(p.strip() for p in from2.split(",") if p.strip())
        if not p2:
            raise RuntimeError("LINK_REPLACE_FROM_2 must contain at least one prefix")
        rules.append((p2, to2))
    return tuple(rules)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_source_channel_id: int
    telegram_proxy_url: str | None

    max_bot_token: str
    max_target_chat_id: int

    link_replace_rules: LinkReplaceRules
    media_group_mode: str
    album_combine_delay: float
    startup_backfill_posts: int
    telegram_catchup_buffer_chat_id: int | None
    telegram_catchup_read_chat_id: int | None

    state_file: str
    retry_attempts: int
    retry_base_delay: float

    @staticmethod
    def load() -> "Settings":
        media_mode = os.getenv("MEDIA_GROUP_MODE", "combined").strip().lower()
        if media_mode not in {"combined", "each", "first_only"}:
            raise RuntimeError("MEDIA_GROUP_MODE must be combined, each, or first_only")

        album_delay = _float("ALBUM_COMBINE_DELAY_SECONDS", 2.0)
        if album_delay <= 0:
            raise RuntimeError("ALBUM_COMBINE_DELAY_SECONDS must be > 0")

        startup_backfill_posts = _int("STARTUP_BACKFILL_POSTS", 0)
        if startup_backfill_posts < 0:
            raise RuntimeError("STARTUP_BACKFILL_POSTS must be >= 0")

        retry_attempts = _int("TELEGRAM_RETRY_ATTEMPTS", 5)
        retry_base_delay = _float("TELEGRAM_RETRY_BASE_DELAY_SECONDS", 2.0)
        if retry_attempts < 1:
            raise RuntimeError("TELEGRAM_RETRY_ATTEMPTS must be >= 1")
        if retry_base_delay <= 0:
            raise RuntimeError("TELEGRAM_RETRY_BASE_DELAY_SECONDS must be > 0")

        state_file = os.getenv("STATE_FILE_V2", "").strip() or os.getenv("STATE_FILE", "").strip()
        if not state_file:
            state_file = "state-v2.json"

        return Settings(
            telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
            telegram_source_channel_id=_int("TELEGRAM_SOURCE_CHANNEL_ID"),
            telegram_proxy_url=os.getenv("TELEGRAM_PROXY_URL", "").strip() or None,
            max_bot_token=_require("MAX_BOT_TOKEN"),
            max_target_chat_id=_int("MAX_TARGET_CHAT_ID"),
            link_replace_rules=_load_link_rules(),
            media_group_mode=media_mode,
            album_combine_delay=album_delay,
            startup_backfill_posts=startup_backfill_posts,
            telegram_catchup_buffer_chat_id=(
                int(os.getenv("TELEGRAM_CATCHUP_BUFFER_CHAT_ID", "").strip())
                if os.getenv("TELEGRAM_CATCHUP_BUFFER_CHAT_ID", "").strip()
                else None
            ),
            telegram_catchup_read_chat_id=(
                int(os.getenv("TELEGRAM_CATCHUP_READ_CHAT_ID", "").strip())
                if os.getenv("TELEGRAM_CATCHUP_READ_CHAT_ID", "").strip()
                else None
            ),
            state_file=state_file,
            retry_attempts=retry_attempts,
            retry_base_delay=retry_base_delay,
        )
