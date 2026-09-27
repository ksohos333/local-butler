"""Telegram ingestion via python-telegram-bot.

Two modes:
1. Polling subscription: bot is added to monitored channels and receives
   messages through the normal update stream.
2. On-startup backfill: fetch the latest N messages from configured public
   channels using the Bot API method getUpdates is not suitable for channels,
   so we use a polling listener and store every forwarded message we see.

For Phase 2 we implement the listener mode. Backfill is a future enhancement.
"""

import hashlib
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from local_butler.apps.geonews.models import Article
from local_butler.core.database import SessionLocal

logger = logging.getLogger(__name__)


def _content_hash(source_id: str, message_text: str, message_id: int | None) -> str:
    payload = f"telegram::{source_id}::{message_id}::{message_text[:240]}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _store_message(
    source_config: dict,
    chat_title: str,
    message_text: str,
    message_id: int,
    date: datetime,
    username: str | None = None,
) -> bool:
    """Persist a Telegram message as a Geonews article.

    Returns True if a new article was created.
    """
    source_id = source_config["source_id"]
    content_hash = _content_hash(source_id, message_text, message_id)

    db = SessionLocal()
    try:
        exists = (
            db.query(Article.id)
            .filter(Article.content_hash == content_hash)
            .first()
        )
        if exists:
            return False

        article = Article(
            source_key=source_id,
            source_type="telegram",
            source_name=chat_title or source_config.get("channel"),
            source_category=source_config.get("category"),
            source_region=source_config.get("region"),
            source_language=source_config.get("language"),
            source_is_primary=True,
            title=message_text[:280],
            summary=message_text[:1024],
            body=message_text,
            url=None,
            published_at=date,
            content_hash=content_hash,
            event_type="other",
            severity=5,
            processed=False,
            metadata_json={
                "telegram_message_id": message_id,
                "telegram_channel": source_config.get("channel"),
                "telegram_username": username,
            },
        )
        db.add(article)
        db.commit()
        return True
    finally:
        db.close()


class TelegramIngestor:
    """Wraps the python-telegram-bot Application and routes channel messages
    to the configured source handlers.
    """

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.application: Application | None = None
        self._monitored_channels: dict[str, dict] = {}

    async def _on_channel_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle a message posted to a monitored channel."""
        if not update.channel_post or not update.channel_post.text:
            return

        channel_id = str(update.channel_post.chat.id)
        channel_username = update.channel_post.chat.username

        # Match by username if we know it
        config = None
        for cfg in self._monitored_channels.values():
            channel_handle = cfg.get("channel", "").lstrip("@").lower()
            if channel_username and channel_username.lower() == channel_handle:
                config = cfg
                break

        if not config:
            logger.debug(
                "Message from unmonitored channel: id=%s username=%s",
                channel_id,
                channel_username,
            )
            return

        message = update.channel_post
        created = _store_message(
            config,
            chat_title=message.chat.title or config["channel"],
            message_text=message.text,
            message_id=message.message_id,
            date=message.date.replace(tzinfo=timezone.utc),
            username=channel_username,
        )
        if created:
            logger.info(
                "Stored Telegram message from %s (id=%s)",
                config["source_id"],
                message.message_id,
            )

    async def _sources_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List monitored channels."""
        lines = []
        for cfg in self._monitored_channels.values():
            lines.append(f"{cfg['channel']} — {cfg.get('region', 'unknown')}")
        text = "Monitored sources:\n" + "\n".join(lines) if lines else "No sources configured."
        await update.message.reply_text(text)

    async def _markets_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Placeholder for market listing."""
        await update.message.reply_text("Prediction markets are coming in Phase 3.")

    async def _help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await update.message.reply_text(
            "Geonews Bot\n"
            "/sources — list monitored channels\n"
            "/markets — list active markets (Phase 3)\n"
            "/help — this message"
        )

    def build_app(self, sources_config: dict[str, dict]) -> Application:
        """Build the PTB Application."""
        self._monitored_channels = {
            k: v for k, v in sources_config.items() if v.get("type") == "telegram"
        }

        if self.application is not None:
            return self.application

        self.application = (
            Application.builder()
            .token(self.bot_token)
            .build()
        )

        self.application.add_handler(
            MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, self._on_channel_message)
        )
        self.application.add_handler(CommandHandler("sources", self._sources_command))
        self.application.add_handler(CommandHandler("markets", self._markets_command))
        self.application.add_handler(CommandHandler("help", self._help_command))
        self.application.add_handler(CommandHandler("start", self._help_command))

        return self.application

    async def start_polling(self) -> None:
        if self.application is None:
            raise RuntimeError("Telegram app not built")
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(
            drop_pending_updates=True,
            poll_interval=1.0,
        )
        logger.info("Telegram bot polling started")

    async def stop(self) -> None:
        if self.application is None:
            return
        try:
            if self.application.updater and self.application.updater.running:
                await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
            logger.info("Telegram bot stopped")
        except RuntimeError as exc:
            logger.warning("Telegram bot stop skipped: %s", exc)


def get_bot_token() -> str | None:
    """Read the Telegram bot token from a config file at a fixed location.

    The file should contain one line: TELEGRAM_BOT_TOKEN=xxx
    """
    import os
    from pathlib import Path

    config_path = Path.home() / ".config" / "geonews" / "telegram.env"
    if config_path.exists():
        content = config_path.read_text()
        for line in content.splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"\'')

    return os.environ.get("TELEGRAM_BOT_TOKEN")
