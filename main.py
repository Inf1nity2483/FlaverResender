from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import UpdateType
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import Message, MessageOriginChannel, Update
from aiogram.utils.text_decorations import html_decoration

from config import Settings
from links import replace_in_html, telegram_inline_keyboard_to_max
from max_api import MaxClient, extract_attachment_token
from state import ProcessedStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("resender.v2")

T = TypeVar("T")
_STARTUP_MAX_ALBUM_ITEMS = 10


@dataclass
class AlbumBuffer:
    chat_id: int
    media_group_id: str
    messages: dict[int, Message] = field(default_factory=dict)
    flush_task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class CachedChannelPost:
    message: Message
    source_chat_id: int
    source_message_id: int


class _ProbeUnknown(RuntimeError):
    pass


class ResenderV2:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        session = AiohttpSession(proxy=settings.telegram_proxy_url) if settings.telegram_proxy_url else None
        self.bot = Bot(token=settings.telegram_bot_token, session=session)
        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)

        self.max_client = MaxClient(settings.max_bot_token, settings.max_target_chat_id)
        self.store = ProcessedStore(settings.state_file)
        self.album_buffers: dict[tuple[int, str], AlbumBuffer] = {}
        self.album_done: set[str] = set()

        self.router.channel_post()(self._on_channel_post)

    @staticmethod
    def _source_ids(msg: Message, fallback_chat_id: int) -> tuple[int, int]:
        origin = msg.forward_origin
        if isinstance(origin, MessageOriginChannel):
            return origin.chat.id, origin.message_id
        return fallback_chat_id, msg.message_id

    @staticmethod
    def _is_missing_message_error(exc: TelegramBadRequest) -> bool:
        text = str(exc).lower()
        return (
            "message to forward not found" in text
            or "message_id_invalid" in text
            or "message not found" in text
        )

    @staticmethod
    def _is_forward_restricted_error(exc: TelegramBadRequest) -> bool:
        text = str(exc).lower()
        return "can't be forwarded" in text or "cannot be forwarded" in text

    async def _telegram_call(self, label: str, factory: Callable[[], Awaitable[T]]) -> T:
        delay = self.settings.retry_base_delay
        last_exc: BaseException | None = None

        for attempt in range(1, self.settings.retry_attempts + 1):
            try:
                return await factory()
            except TelegramRetryAfter as exc:
                last_exc = exc
                wait = float(exc.retry_after) + 0.5
                logger.warning("%s rate-limited, wait %.1fs (%s/%s)", label, wait, attempt, self.settings.retry_attempts)
                await asyncio.sleep(wait)
            except TelegramNetworkError as exc:
                last_exc = exc
                if attempt >= self.settings.retry_attempts:
                    break
                logger.warning("%s network error: %s (%s/%s), retry in %.1fs", label, exc, attempt, self.settings.retry_attempts, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

        assert last_exc is not None
        raise last_exc

    @staticmethod
    def _message_html(msg: Message) -> str | None:
        if msg.text:
            return html_decoration.unparse(msg.text, msg.entities or [])
        if msg.caption:
            return html_decoration.unparse(msg.caption, msg.caption_entities or [])
        return None

    def _transform_html(self, html: str | None) -> str | None:
        if not html:
            return None
        return replace_in_html(html, self.settings.link_replace_rules)

    def _keyboard_max(self, msg: Message) -> list[dict[str, Any]] | None:
        return telegram_inline_keyboard_to_max(msg.reply_markup, self.settings.link_replace_rules)

    async def _download_file_bytes(self, file_id: str) -> bytes:
        file_info = await self._telegram_call("getFile", lambda: self.bot.get_file(file_id))
        if not file_info.file_path:
            raise RuntimeError(f"Telegram getFile returned empty file_path for {file_id}")
        buffer = io.BytesIO()
        await self._telegram_call(
            "download_file",
            lambda: self.bot.download_file(file_info.file_path, destination=buffer),
        )
        return buffer.getvalue()

    @staticmethod
    def _guess_upload_type(msg: Message) -> tuple[str, str, str] | None:
        if msg.photo:
            return "image", "photo.jpg", msg.photo[-1].file_id
        if msg.video:
            name = (msg.video.file_name or "video.mp4")[-200:]
            return "video", name, msg.video.file_id
        if msg.animation:
            name = (msg.animation.file_name or "animation.mp4")[-200:]
            return "video", name, msg.animation.file_id
        if msg.document:
            doc = msg.document
            name = (doc.file_name or "file.bin")[-200:]
            mime = (doc.mime_type or "").lower()
            if mime.startswith("image/"):
                return "image", name, doc.file_id
            if mime.startswith("video/"):
                return "video", name, doc.file_id
            if mime.startswith("audio/") or mime == "application/ogg":
                return "audio", name, doc.file_id
            return "file", name, doc.file_id
        if msg.audio:
            name = (msg.audio.file_name or "audio.mp3")[-200:]
            return "audio", name, msg.audio.file_id
        if msg.voice:
            return "audio", "voice.oga", msg.voice.file_id
        return None

    @staticmethod
    def _has_media(msg: Message) -> bool:
        return bool(
            msg.photo
            or msg.video
            or msg.animation
            or msg.document
            or msg.audio
            or msg.voice
        )

    @staticmethod
    def _has_text_or_keyboard(msg: Message) -> bool:
        return bool((msg.text and msg.text.strip()) or (msg.caption and msg.caption.strip()) or msg.reply_markup)

    async def _send_with_media(
        self,
        *,
        text: str | None,
        upload_type: str,
        filename: str,
        file_id: str,
        keyboard: list[dict[str, Any]] | None,
    ) -> None:
        raw = await self._download_file_bytes(file_id)
        uploaded = await self.max_client.upload_bytes(raw, filename=filename, upload_type=upload_type)
        token = extract_attachment_token(uploaded)

        att_type = "image" if upload_type == "image" else upload_type
        attachments: list[dict[str, Any]] = [{"type": att_type, "payload": {"token": token}}]
        if keyboard:
            attachments.extend(keyboard)
        await self.max_client.send_message(text, attachments=attachments)

    async def _send_with_multiple_media(
        self,
        *,
        text: str | None,
        items: list[tuple[str, str, str]],
        keyboard: list[dict[str, Any]] | None,
    ) -> None:
        attachments: list[dict[str, Any]] = []
        for upload_type, filename, file_id in items:
            raw = await self._download_file_bytes(file_id)
            uploaded = await self.max_client.upload_bytes(raw, filename=filename, upload_type=upload_type)
            token = extract_attachment_token(uploaded)
            att_type = "image" if upload_type == "image" else upload_type
            attachments.append({"type": att_type, "payload": {"token": token}})

        if keyboard:
            attachments.extend(keyboard)
        await self.max_client.send_message(text, attachments=attachments)

    def _album_caption_html(self, messages: list[Message]) -> str | None:
        for message in messages:
            text = self._transform_html(self._message_html(message))
            if text and text.strip():
                return text
        return None

    def _album_keyboard(self, messages: list[Message]) -> list[dict[str, Any]] | None:
        for message in messages:
            keyboard = self._keyboard_max(message)
            if keyboard:
                return keyboard
        return None

    async def _forward_single(
        self,
        msg: Message,
        *,
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
    ) -> bool:
        if source_chat_id is None or source_message_id is None:
            source_chat_id, source_message_id = self._source_ids(
                msg, self.settings.telegram_source_channel_id
            )
        if self.store.contains(source_chat_id, source_message_id):
            return False

        mgid = msg.media_group_id
        if self.settings.media_group_mode == "first_only" and mgid:
            if mgid in self.album_done:
                self.store.add(source_chat_id, source_message_id)
                return False
            self.album_done.add(mgid)

        html = self._transform_html(self._message_html(msg))
        keyboard = self._keyboard_max(msg)
        guess = self._guess_upload_type(msg)
        if guess:
            upload_type, filename, file_id = guess
            await self._send_with_media(
                text=html if html and html.strip() else None,
                upload_type=upload_type,
                filename=filename,
                file_id=file_id,
                keyboard=keyboard,
            )
        else:
            attachments = keyboard[:] if keyboard else None
            text_out = html if html and html.strip() else None
            if not text_out and not attachments:
                self.store.add(source_chat_id, source_message_id)
                logger.info("skip empty post chat=%s mid=%s", source_chat_id, source_message_id)
                return False
            await self.max_client.send_message(text_out, attachments=attachments)

        self.store.add(source_chat_id, source_message_id)
        logger.info("forwarded single chat=%s mid=%s", source_chat_id, source_message_id)
        return True

    async def _forward_album(
        self,
        messages: list[Message],
        *,
        source_chat_id: int | None = None,
        source_message_ids: list[int] | None = None,
    ) -> bool:
        if not messages:
            return False
        if source_chat_id is None or source_message_ids is None:
            ordered = sorted(messages, key=lambda m: m.message_id)
            source_pairs = [
                self._source_ids(msg, self.settings.telegram_source_channel_id)
                for msg in ordered
            ]
            source_chat_id = source_pairs[0][0]
            source_ids = [mid for _, mid in source_pairs]
        else:
            if len(source_message_ids) != len(messages):
                raise ValueError("source_message_ids length must match messages length")
            paired = sorted(zip(messages, source_message_ids, strict=True), key=lambda p: p[1])
            ordered = [msg for msg, _ in paired]
            source_ids = [mid for _, mid in paired]
        if all(self.store.contains(source_chat_id, mid) for mid in source_ids):
            return False

        html = self._album_caption_html(ordered)
        keyboard = self._album_keyboard(ordered)
        items: list[tuple[str, str, str]] = []
        for msg in ordered:
            guess = self._guess_upload_type(msg)
            if guess:
                items.append(guess)

        if items:
            await self._send_with_multiple_media(
                text=html if html and html.strip() else None,
                items=items,
                keyboard=keyboard,
            )
        else:
            attachments = keyboard[:] if keyboard else None
            text_out = html if html and html.strip() else None
            if not text_out and not attachments:
                for mid in source_ids:
                    self.store.add(source_chat_id, mid)
                logger.info("skip empty album chat=%s mids=%s", source_chat_id, source_ids)
                return False
            await self.max_client.send_message(text_out, attachments=attachments)

        for mid in source_ids:
            self.store.add(source_chat_id, mid)
        logger.info("forwarded album chat=%s mids=%s", source_chat_id, source_ids)
        return True

    @staticmethod
    def _build_startup_units(
        posts: list[CachedChannelPost], mode: str
    ) -> list[list[CachedChannelPost]]:
        def _chunk(group: list[CachedChannelPost]) -> list[list[CachedChannelPost]]:
            if len(group) <= _STARTUP_MAX_ALBUM_ITEMS:
                return [group]
            chunks: list[list[CachedChannelPost]] = []
            for idx in range(0, len(group), _STARTUP_MAX_ALBUM_ITEMS):
                chunks.append(group[idx : idx + _STARTUP_MAX_ALBUM_ITEMS])
            return chunks

        ordered = sorted(posts, key=lambda p: p.source_message_id)
        if mode == "each":
            return [[p] for p in ordered]

        grouped: dict[str, list[CachedChannelPost]] = {}
        for post in ordered:
            mgid = post.message.media_group_id
            if mgid:
                grouped.setdefault(mgid, []).append(post)

        units: list[list[CachedChannelPost]] = []
        consumed_ids: set[int] = set()
        for group in grouped.values():
            sorted_group = sorted(group, key=lambda p: p.source_message_id)
            if len(sorted_group) > 1:
                units.extend(_chunk(sorted_group))
                consumed_ids.update(p.source_message_id for p in sorted_group)

        remaining = [p for p in ordered if p.source_message_id not in consumed_ids]
        if mode == "combined":
            # Conservative fallback: only build pseudo-album when first message has
            # text/caption/keyboard and following contiguous media messages do not.
            # This avoids mixing independent posts into one giant album.
            i = 0
            while i < len(remaining):
                current = remaining[i]
                run = [current]
                j = i + 1
                can_fallback_album = (
                    ResenderV2._has_media(current.message)
                    and ResenderV2._has_text_or_keyboard(current.message)
                )
                if can_fallback_album:
                    while j < len(remaining):
                        nxt = remaining[j]
                        prev = run[-1]
                        if (
                            ResenderV2._has_media(prev.message)
                            and ResenderV2._has_media(nxt.message)
                            and nxt.source_message_id == prev.source_message_id + 1
                            and not ResenderV2._has_text_or_keyboard(nxt.message)
                        ):
                            run.append(nxt)
                            j += 1
                            continue
                        break

                if len(run) > 1:
                    units.extend(_chunk(run))
                    i = j
                else:
                    units.append([current])
                    i += 1
        elif mode == "first_only":
            units.extend([[p] for p in remaining])
        else:
            units.extend([[p] for p in remaining])

        if mode == "first_only":
            units = [[sorted(unit, key=lambda p: p.source_message_id)[0]] for unit in units]

        units.sort(key=lambda unit: unit[0].source_message_id)
        return units

    async def _process_startup_messages(self, posts: list[CachedChannelPost]) -> None:
        if not posts:
            return
        units = self._build_startup_units(posts, self.settings.media_group_mode)
        sent = 0
        for unit in units:
            mids = [p.source_message_id for p in unit]
            logger.info(
                "startup unit: kind=%s mids=%s",
                "album" if len(unit) > 1 else "single",
                mids,
            )
            try:
                if len(unit) == 1:
                    post = unit[0]
                    if await self._forward_single(
                        post.message,
                        source_chat_id=post.source_chat_id,
                        source_message_id=post.source_message_id,
                    ):
                        sent += 1
                else:
                    if await self._forward_album(
                        [p.message for p in unit],
                        source_chat_id=unit[0].source_chat_id,
                        source_message_ids=mids,
                    ):
                        sent += 1
            except Exception as exc:
                logger.error("startup unit failed mids=%s error=%s", mids, exc)
        logger.info("startup processing done: %s unit(s) forwarded", sent)

    async def _delete_message_quietly(self, chat_id: int, message_id: int) -> None:
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass

    async def _channel_message_exists(
        self, *, channel_id: int, buffer_chat_id: int, message_id: int
    ) -> bool:
        try:
            sent = await self._telegram_call(
                "forwardMessage (exists probe)",
                lambda: self.bot.forward_message(
                    chat_id=buffer_chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id,
                    disable_notification=True,
                ),
            )
            await self._delete_message_quietly(buffer_chat_id, sent.message_id)
            return True
        except TelegramBadRequest as exc:
            if self._is_missing_message_error(exc):
                return False
            if self._is_forward_restricted_error(exc):
                # Fallback probe for protected channels.
                try:
                    copied = await self._telegram_call(
                        "copyMessage (exists probe fallback)",
                        lambda: self.bot.copy_message(
                            chat_id=buffer_chat_id,
                            from_chat_id=channel_id,
                            message_id=message_id,
                            disable_notification=True,
                        ),
                    )
                    await self._delete_message_quietly(buffer_chat_id, copied.message_id)
                    return True
                except TelegramBadRequest as inner:
                    if self._is_missing_message_error(inner):
                        return False
                    raise _ProbeUnknown(
                        f"probe fallback failed for channel_id={channel_id} "
                        f"message_id={message_id}: {inner}"
                    )
            raise

    async def _find_latest_channel_message_id(self, *, channel_id: int, buffer_chat_id: int) -> int | None:
        hints: list[int] = []
        stored = self.store.max_message_id(channel_id)
        if stored is not None:
            hints.append(stored)

        try:
            chat = await self._telegram_call("get_chat (latest hint)", lambda: self.bot.get_chat(channel_id))
            if chat.pinned_message:
                hints.append(chat.pinned_message.message_id)
        except Exception as exc:
            logger.warning("startup catch-up: cannot read pinned message hint: %s", exc)

        if not hints:
            return None

        latest = max(hints)
        consecutive_misses = 0
        probe_window = max(60, self.settings.startup_backfill_posts * 3)
        for candidate in range(latest + 1, latest + probe_window + 1):
            try:
                exists = await self._channel_message_exists(
                    channel_id=channel_id,
                    buffer_chat_id=buffer_chat_id,
                    message_id=candidate,
                )
            except _ProbeUnknown as exc:
                fallback_latest = latest + max(
                    60, self.settings.startup_backfill_posts * 2 + 20
                )
                logger.warning(
                    "startup catch-up probe uncertain (%s); fallback latest=%s",
                    exc,
                    fallback_latest,
                )
                latest = max(latest, fallback_latest)
                break
            if exists:
                latest = candidate
                consecutive_misses = 0
            else:
                consecutive_misses += 1
                if consecutive_misses >= 10:
                    break
        return latest

    async def _fetch_channel_post_message(
        self,
        *,
        channel_id: int,
        buffer_chat_id: int,
        read_chat_id: int | None,
        message_id: int,
    ) -> CachedChannelPost | None:
        try:
            sent = await self._telegram_call(
                "forwardMessage (catch-up fetch)",
                lambda: self.bot.forward_message(
                    chat_id=buffer_chat_id,
                    from_chat_id=channel_id,
                    message_id=message_id,
                    disable_notification=True,
                ),
            )
            await self._delete_message_quietly(buffer_chat_id, sent.message_id)
            src_chat, src_mid = self._source_ids(sent, channel_id)
            return CachedChannelPost(sent, src_chat, src_mid)
        except TelegramBadRequest as exc:
            if self._is_missing_message_error(exc):
                return None
            if self._is_forward_restricted_error(exc):
                if read_chat_id is None:
                    logger.warning(
                        "startup catch-up: channel_id=%s message_id=%s restricted forward and "
                        "no TELEGRAM_CATCHUP_READ_CHAT_ID set; skipping",
                        channel_id,
                        message_id,
                    )
                    return None
                try:
                    copied = await self._telegram_call(
                        "copyMessage (catch-up fallback)",
                        lambda: self.bot.copy_message(
                            chat_id=buffer_chat_id,
                            from_chat_id=channel_id,
                            message_id=message_id,
                            disable_notification=True,
                        ),
                    )
                    sent = await self._telegram_call(
                        "forwardMessage (catch-up read fallback)",
                        lambda: self.bot.forward_message(
                            chat_id=read_chat_id,
                            from_chat_id=buffer_chat_id,
                            message_id=copied.message_id,
                            disable_notification=True,
                        ),
                    )
                    await self._delete_message_quietly(read_chat_id, sent.message_id)
                    await self._delete_message_quietly(buffer_chat_id, copied.message_id)
                    return CachedChannelPost(sent, channel_id, message_id)
                except TelegramBadRequest as inner:
                    logger.warning(
                        "startup catch-up: channel_id=%s message_id=%s fallback failed: %s",
                        channel_id,
                        message_id,
                        inner,
                    )
                    return None
            logger.warning(
                "startup catch-up: channel_id=%s message_id=%s fetch failed: %s",
                channel_id,
                message_id,
                exc,
            )
            return None

    async def _startup_catchup_recent_posts(self) -> None:
        count = self.settings.startup_backfill_posts
        if count <= 0:
            return

        buffer_chat_id = self.settings.telegram_catchup_buffer_chat_id
        if buffer_chat_id is None:
            logger.warning(
                "startup catch-up skipped: set TELEGRAM_CATCHUP_BUFFER_CHAT_ID to check last %s posts",
                count,
            )
            return

        channel_id = self.settings.telegram_source_channel_id
        read_chat_id = self.settings.telegram_catchup_read_chat_id
        logger.info("startup catch-up: checking last %s channel post(s)", count)

        latest = await self._find_latest_channel_message_id(
            channel_id=channel_id,
            buffer_chat_id=buffer_chat_id,
        )
        if latest is None:
            logger.info("startup catch-up skipped: latest message_id hint not found")
            return

        collected: list[CachedChannelPost] = []
        for mid in range(latest, max(0, latest - (count * 4)), -1):
            post = await self._fetch_channel_post_message(
                channel_id=channel_id,
                buffer_chat_id=buffer_chat_id,
                read_chat_id=read_chat_id,
                message_id=mid,
            )
            if post is not None:
                collected.append(post)
                if len(collected) >= count:
                    break

        if not collected:
            logger.info("startup catch-up: no retrievable posts near message_id=%s", latest)
            return

        collected.sort(key=lambda p: p.source_message_id)
        logger.info("startup catch-up: collected %s post(s) near latest=%s", len(collected), latest)
        await self._process_startup_messages(collected)

    async def _flush_album(self, key: tuple[int, str]) -> None:
        buf = self.album_buffers.pop(key, None)
        if not buf:
            return
        messages = sorted(buf.messages.values(), key=lambda m: m.message_id)
        try:
            await self._forward_album(messages)
        except Exception as exc:
            logger.error("album flush failed chat=%s mgid=%s error=%s", key[0], key[1], exc)

    def _schedule_album(self, msg: Message) -> None:
        mgid = msg.media_group_id
        if not mgid:
            return
        key = (msg.chat.id, mgid)
        buf = self.album_buffers.get(key)
        if buf is None:
            buf = AlbumBuffer(chat_id=msg.chat.id, media_group_id=mgid)
            self.album_buffers[key] = buf
        buf.messages[msg.message_id] = msg

        if buf.flush_task and not buf.flush_task.done():
            buf.flush_task.cancel()

        async def _flush_later() -> None:
            try:
                await asyncio.sleep(self.settings.album_combine_delay)
                await self._flush_album(key)
            except asyncio.CancelledError:
                return

        buf.flush_task = asyncio.create_task(_flush_later())

    async def _process_live_message(self, msg: Message) -> None:
        if msg.chat.id != self.settings.telegram_source_channel_id:
            return

        if self.settings.media_group_mode == "combined" and msg.media_group_id:
            self._schedule_album(msg)
            return

        try:
            await self._forward_single(msg)
        except Exception as exc:
            logger.error("live post failed mid=%s error=%s", msg.message_id, exc)

    async def _drain_pending_channel_posts(self) -> None:
        logger.info("startup drain: reading pending channel_post updates")
        offset = 0
        collected: list[Message] = []

        try:
            while True:
                updates = await self._telegram_call(
                    "get_updates (startup drain)",
                    lambda: self.bot.get_updates(
                        offset=offset,
                        limit=100,
                        timeout=0,
                        allowed_updates=[UpdateType.CHANNEL_POST],
                    ),
                )
                if not updates:
                    break
                for upd in updates:
                    offset = upd.update_id + 1
                    if isinstance(upd, Update) and upd.channel_post is not None:
                        msg = upd.channel_post
                        if msg.chat.id == self.settings.telegram_source_channel_id:
                            collected.append(msg)
                if len(updates) < 100:
                    break

            if offset > 0:
                await self._telegram_call(
                    "get_updates (startup ack)",
                    lambda: self.bot.get_updates(
                        offset=offset,
                        limit=1,
                        timeout=0,
                        allowed_updates=[UpdateType.CHANNEL_POST],
                    ),
                )
        except Exception as exc:
            logger.error("startup drain failed, continue polling without drain: %s", exc)
            return

        if self.settings.startup_backfill_posts > 0 and len(collected) > self.settings.startup_backfill_posts:
            collected = collected[-self.settings.startup_backfill_posts :]

        if collected:
            logger.info("startup drain: %s channel post(s) queued for forwarding", len(collected))
            await self._process_startup_messages(collected)
        else:
            logger.info("startup drain: no pending channel posts")

    async def _on_channel_post(self, msg: Message) -> None:
        await self._process_live_message(msg)

    async def run(self) -> None:
        try:
            me = await self._telegram_call("getMe", self.bot.get_me)
            logger.info("bot started as @%s", me.username or "?")
            await self.bot.delete_webhook(drop_pending_updates=False)
            await self._drain_pending_channel_posts()
            await self._startup_catchup_recent_posts()
            await self.dp.start_polling(
                self.bot,
                allowed_updates=[UpdateType.CHANNEL_POST],
            )
        finally:
            for buf in self.album_buffers.values():
                if buf.flush_task and not buf.flush_task.done():
                    buf.flush_task.cancel()
            await self.max_client.aclose()
            await self.bot.session.close()


async def _amain() -> None:
    settings = Settings.load()
    if settings.telegram_proxy_url:
        logger.info("Telegram client uses proxy for Bot API and file downloads")
    logger.info(
        "starting polling: source channel=%s mode=%s startup_backfill_posts=%s state_file=%s",
        settings.telegram_source_channel_id,
        settings.media_group_mode,
        settings.startup_backfill_posts,
        settings.state_file,
    )
    app = ResenderV2(settings)
    await app.run()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
