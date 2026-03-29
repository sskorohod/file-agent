"""Telegram bot handlers — receive files, process in background, send results."""

from __future__ import annotations

import asyncio
import logging
import time
from functools import wraps
from io import BytesIO
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.config import get_settings
from app.pipeline import Pipeline

logger = logging.getLogger(__name__)

# ── Rate limiting ─────────────────────────────────────────────────────────
_last_command: dict[int, float] = {}
_MIN_COMMAND_INTERVAL = 1.0  # seconds


async def _safe_answer(query, text: str = "", **kwargs):
    """Answer callback query, ignoring 'query too old' errors."""
    try:
        await query.answer(text, **kwargs)
    except Exception as e:
        if "query is too old" in str(e).lower() or "query id is invalid" in str(e).lower():
            logger.debug(f"Callback query expired (normal): {e}")
        else:
            logger.warning(f"Failed to answer callback query: {e}")


async def _safe_edit(query, text: str, **kwargs):
    """Edit callback message, falling back to send_message on error."""
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception as e:
        if "message is not modified" in str(e).lower():
            pass  # Same text — ignore
        else:
            logger.warning(f"Failed to edit message: {e}")
            try:
                await query.message.reply_text(text, **kwargs)
            except Exception:
                pass


def owner_only(func):
    """Restrict handler to the configured owner_id only. Block non-private chats."""
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Block group/channel chats — only private allowed
        if update.effective_chat and update.effective_chat.type != "private":
            return

        owner_id = get_settings().telegram.owner_id
        user_id = update.effective_user.id if update.effective_user else 0
        if not owner_id or user_id != owner_id:
            if update.callback_query:
                await update.callback_query.answer("Бот приватный.", show_alert=True)
            elif update.message:
                await update.message.reply_text("🔒 Бот приватный.")
            return

        # Rate limiting
        now = time.monotonic()
        if now - _last_command.get(user_id, 0) < _MIN_COMMAND_INTERVAL:
            return
        _last_command[user_id] = now

        return await func(self, update, context)
    return wrapper

def _check_pin(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Check if PIN is required and extract it from args. Returns error msg or None."""
    import secrets as _sec
    pin = get_settings().telegram.pin_code
    if not pin:
        return None  # PIN not configured
    args = context.args or []
    if not args or not _sec.compare_digest(args[-1], pin):
        return "🔑 Эта операция требует PIN-код.\nДобавьте PIN в конце команды."
    # Remove PIN from args so handlers don't see it
    context.args = args[:-1]
    return None


# ── Chat ID storage (encrypted in DB) ────────────────────────────────────
_CHAT_ID_FILE = Path("data/chat_id.txt")  # Legacy — kept for migration


def _save_chat_id(chat_id: int):
    """Save chat_id to DB (encrypted) if available, fallback to file."""
    from app.main import get_state
    db = get_state("db")
    if db:
        import asyncio as _aio
        try:
            loop = _aio.get_running_loop()
            loop.create_task(_save_chat_id_async(db, chat_id))
        except RuntimeError:
            # No event loop — fallback to file
            _CHAT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
            _CHAT_ID_FILE.write_text(str(chat_id))
    else:
        _CHAT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CHAT_ID_FILE.write_text(str(chat_id))


async def _save_chat_id_async(db, chat_id: int):
    """Save chat_id to encrypted secrets table."""
    from app.utils.crypto import encrypt
    session_secret = get_settings().web.session_secret
    if session_secret:
        await db.set_secret("OWNER_CHAT_ID", encrypt(str(chat_id), session_secret))
    else:
        await db.set_secret("OWNER_CHAT_ID", str(chat_id))


def get_owner_chat_id() -> int | None:
    """Get owner chat_id — sync version for background tasks."""
    # Try legacy file first (will be migrated)
    if _CHAT_ID_FILE.exists():
        try:
            return int(_CHAT_ID_FILE.read_text().strip())
        except ValueError:
            pass
    return None


async def get_owner_chat_id_async(db=None) -> int | None:
    """Get owner chat_id from DB (preferred) or file (fallback)."""
    if db:
        raw = await db.get_secret("OWNER_CHAT_ID")
        if raw:
            # Try to decrypt
            session_secret = get_settings().web.session_secret
            if session_secret:
                from app.utils.crypto import decrypt
                decrypted = decrypt(raw, session_secret)
                if decrypted:
                    try:
                        return int(decrypted)
                    except ValueError:
                        pass
            # Not encrypted or no session secret
            try:
                return int(raw)
            except ValueError:
                pass
    return get_owner_chat_id()

# Max file size for Telegram download (20MB)
MAX_TELEGRAM_FILE_SIZE = 20 * 1024 * 1024


def _log_task_exception(task: asyncio.Task):
    """Callback to log unhandled exceptions in background tasks."""
    if not task.cancelled() and task.exception():
        logger.error(f"Background task failed: {task.exception()}", exc_info=task.exception())


class BotHandlers:
    """Telegram bot handlers — wires up commands and file reception."""

    def __init__(self, pipeline: Pipeline, search_fn=None, analytics_fn=None):
        self.pipeline = pipeline
        self.search_fn = search_fn  # injected from LLM search module
        self.analytics_fn = analytics_fn  # injected from LLM analytics module
        self._pending_files: dict[str, str] = {}  # short_key → full file_id

    def _schedule_delete(self, message, context=None):
        """Schedule auto-deletion of a bot message after configured delay."""
        delay = get_settings().telegram.auto_delete_seconds
        if delay <= 0 or not message:
            return

        async def _do_delete():
            await asyncio.sleep(delay)
            try:
                await message.delete()
            except Exception:
                pass

        asyncio.ensure_future(_do_delete())

    COMMANDS = [
        BotCommand("start", "Начать работу"),
        BotCommand("help", "Список команд"),
        BotCommand("search", "Семантический поиск"),
        BotCommand("files", "Список файлов"),
        BotCommand("scan", "Многостраничное сканирование"),
        BotCommand("done", "Завершить и проверить"),
        BotCommand("cancel", "Отменить сканирование"),
        BotCommand("recent", "Последние файлы"),
        BotCommand("stats", "Статистика базы"),
        BotCommand("skills", "Список скиллов"),
        BotCommand("analytics", "Аналитика документов"),
        BotCommand("insights", "AI обзор и рекомендации"),
        BotCommand("notes", "Заметки"),
        BotCommand("note", "Быстрая заметка"),
        BotCommand("today", "Метрики дня"),
        BotCommand("missing", "Что не заполнено"),
        BotCommand("morning", "Утренний брифинг"),
        BotCommand("habits", "Привычки и стрики"),
        BotCommand("export", "Экспорт заметок"),
        BotCommand("analyze", "Недельный анализ"),
        BotCommand("unlock", "Разблокировать шифрование"),
    ]

    def register(self, app: Application):
        """Register all handlers with the bot application."""
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("search", self.cmd_search))
        app.add_handler(CommandHandler("files", self.cmd_files))
        app.add_handler(CommandHandler("scan", self.cmd_scan))
        app.add_handler(CommandHandler("done", self.cmd_done))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CommandHandler("recent", self.cmd_recent))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("analytics", self.cmd_analytics))
        app.add_handler(CommandHandler("skills", self.cmd_skills))
        app.add_handler(CommandHandler("insights", self.cmd_insights))
        app.add_handler(CommandHandler("notes", self.cmd_notes))
        app.add_handler(CommandHandler("note", self.cmd_note))
        app.add_handler(CommandHandler("today", self.cmd_today))
        app.add_handler(CommandHandler("missing", self.cmd_missing))
        app.add_handler(CommandHandler("morning", self.cmd_morning))
        app.add_handler(CommandHandler("habits", self.cmd_habits))
        app.add_handler(CommandHandler("export", self.cmd_export))
        app.add_handler(CommandHandler("analyze", self.cmd_analyze))
        app.add_handler(CommandHandler("unlock", self.cmd_unlock))

        # File handlers (documents, photos)
        app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))

        # Voice messages
        app.add_handler(MessageHandler(filters.VOICE, self.handle_voice))

        # Callback handlers
        app.add_handler(CallbackQueryHandler(self.handle_scan_confirm, pattern="^scan:"))
        app.add_handler(CallbackQueryHandler(self.handle_files_page, pattern="^fp:"))
        app.add_handler(CallbackQueryHandler(self.handle_voice_choice, pattern="^vc:"))
        app.add_handler(CallbackQueryHandler(self.handle_text_choice, pattern="^tc:"))
        app.add_handler(CallbackQueryHandler(self.handle_reminder_action, pattern="^rem:"))
        app.add_handler(CallbackQueryHandler(self.handle_note_reminder_action, pattern="^nrem:"))
        app.add_handler(CallbackQueryHandler(self.handle_note_action, pattern="^note:"))
        app.add_handler(CallbackQueryHandler(self.handle_checkin_callback, pattern="^ci:"))
        app.add_handler(CallbackQueryHandler(self.handle_dedup_choice, pattern="^dedup:"))
        app.add_handler(CallbackQueryHandler(self.handle_file_send, pattern="^file:"))

        # Text messages → search / Q&A
        app.add_handler(MessageHandler(
            filters.TEXT & ~filters.COMMAND, self.handle_text
        ))

        # Global error handler — send errors to owner chat
        app.add_error_handler(self._error_handler)

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Send bot errors to owner via Telegram."""
        logger.error(f"Bot error: {context.error}", exc_info=context.error)
        try:
            chat_id = get_owner_chat_id()
            if chat_id:
                error_text = str(context.error)[:500]
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Bot error:\n<code>{error_text}</code>",
                    parse_mode="HTML",
                )
        except Exception:
            pass

    # ── Commands ────────────────────────────────────────────────────────

    @owner_only
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Save chat_id for reminders
        _save_chat_id(update.message.chat_id)
        await update.message.reply_text(
            "👋 Привет! Я AI File Agent.\n\n"
            "Отправь мне файл (PDF, фото, DOCX) — я распознаю, классифицирую "
            "и сохраню в базу знаний.\n\n"
            "Затем можешь задать вопрос текстом — найду ответ по твоим документам.\n\n"
            "Команды: /help, /search, /recent, /stats, /skills"
        )

    @owner_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📖 Команды:\n\n"
            "/search <запрос> — семантический поиск\n"
            "/files [категория] — список файлов\n"
            "/scan [название] — многостраничное сканирование\n"
            "/done — завершить сканирование\n"
            "/recent [N] — последние N файлов\n"
            "/stats — статистика базы\n"
            "/skills — список скиллов\n"
            "/notes — заметки\n\n"
            "📎 Просто отправь файл — я обработаю.\n"
            "📸 Отправь несколько фото сразу — соберу в PDF.\n"
            "💬 Напиши вопрос — найду ответ в твоих документах."
        )

    # ── Scan: multi-page document ────────────────────────────────────

    @owner_only
    async def cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Start multi-page scan session."""
        from datetime import datetime
        name = " ".join(context.args) if context.args else "Scan"
        context.user_data["scan_session"] = {
            "name": name, "images": [], "started_at": datetime.now(),
        }
        await update.message.reply_text(
            f"📸 Сканирование: «{name}»\n\n"
            f"Отправляйте фото страниц по одному.\n"
            f"/done — завершить и обработать\n"
            f"/cancel — отменить сканирование"
        )

    @owner_only
    async def cmd_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show scan preview with confirm/cancel buttons."""
        session = context.user_data.get("scan_session")
        if not session or not session["images"]:
            await update.message.reply_text("📭 Нет активной сессии сканирования.")
            return

        n = len(session["images"])
        name = session["name"]

        # Send preview: thumbnails of all pages as a strip
        try:
            from PIL import Image
            from io import BytesIO as _BytesIO

            # Build preview strip: small thumbnails side by side
            thumbs = []
            for data in session["images"]:
                img = Image.open(_BytesIO(data))
                img.thumbnail((150, 200))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                thumbs.append(img)

            # Compose strip
            total_w = sum(t.width for t in thumbs) + (len(thumbs) - 1) * 4
            max_h = max(t.height for t in thumbs)
            strip = Image.new("RGB", (total_w, max_h + 20), (21, 21, 21))

            # Add page numbers
            x = 0
            for i, t in enumerate(thumbs):
                strip.paste(t, (x, 0))
                x += t.width + 4

            buf = _BytesIO()
            strip.save(buf, format="JPEG", quality=85)
            buf.seek(0)

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"✅ Собрать PDF ({n} стр.)", callback_data="scan:confirm"),
                    InlineKeyboardButton("❌ Отмена", callback_data="scan:cancel"),
                ]
            ])

            await update.message.reply_photo(
                photo=buf,
                caption=f"📸 «{name}» — {n} страниц\n\nПорядок: слева → направо.\nВсё верно?",
                reply_markup=keyboard,
            )
        except Exception as e:
            # Fallback without preview image
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(f"✅ Собрать PDF ({n} стр.)", callback_data="scan:confirm"),
                    InlineKeyboardButton("❌ Отмена", callback_data="scan:cancel"),
                ]
            ])
            await update.message.reply_text(
                f"📸 «{name}» — {n} страниц\nСобрать PDF?",
                reply_markup=keyboard,
            )

    @owner_only
    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Cancel active scan session."""
        session = context.user_data.pop("scan_session", None)
        if session:
            n = len(session["images"])
            await update.message.reply_text(f"🚫 Сканирование «{session['name']}» отменено ({n} стр. удалено).")
        else:
            await update.message.reply_text("📭 Нет активной сессии.")

    @owner_only
    async def handle_scan_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle scan confirm/cancel buttons."""
        query = update.callback_query
        await _safe_answer(query)
        action = query.data.split(":")[1]  # "confirm" or "cancel"

        session = context.user_data.pop("scan_session", None)

        if action == "cancel":
            try:
                msg = f"🚫 Сканирование «{session['name']}» отменено." if session else "🚫 Отменено."
                await query.edit_message_caption(msg)
            except Exception:
                await _safe_edit(query, "🚫 Отменено.")
            return

        if not session or not session["images"]:
            try:
                await query.edit_message_caption("📭 Данные устарели. Начните /scan заново.")
            except Exception:
                await _safe_edit(query, "📭 Данные устарели.")
            return

        # Confirm: build PDF and process
        from app.utils.pdf import images_to_pdf
        n = len(session["images"])
        filename = f"{session['name']}.pdf"

        try:
            await query.edit_message_caption(f"⏳ Собираю {n} стр. в PDF: «{filename}»...")
        except Exception:
            pass

        ack = await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"⏳ Обрабатываю «{filename}»..."
        )

        try:
            pdf_bytes = images_to_pdf(session["images"])
        except Exception as e:
            await ack.edit_text(f"❌ Ошибка сборки PDF: {e}")
            return

        task = asyncio.create_task(self._process_and_reply(ack, pdf_bytes, filename, context))
        task.add_done_callback(_log_task_exception)

    _FILES_PER_PAGE = 10

    @owner_only
    async def cmd_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List files with pagination and optional category filter."""
        category = " ".join(context.args) if context.args else None
        text, markup = await self._files_page(0, category)
        await update.message.reply_text(text, reply_markup=markup)

    @owner_only
    async def handle_files_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle pagination buttons for /files."""
        query = update.callback_query
        await _safe_answer(query)
        # fp:<offset>:<category or _>
        parts = query.data.split(":", 2)
        offset = int(parts[1]) if len(parts) > 1 else 0
        category = parts[2] if len(parts) > 2 and parts[2] != "_" else None
        text, markup = await self._files_page(offset, category)
        await _safe_edit(query, text, reply_markup=markup)

    async def _files_page(self, offset: int, category: str | None):
        """Build a files list page with navigation."""
        db = self.pipeline.db
        files = await db.list_files(category=category, limit=self._FILES_PER_PAGE + 1, offset=offset)
        has_next = len(files) > self._FILES_PER_PAGE
        files = files[:self._FILES_PER_PAGE]

        stats = await db.get_stats()
        total = stats["total_files"]
        cat_label = f" [{category}]" if category else ""
        header = f"📂 Файлы{cat_label} ({total}):\n\n"

        if not files:
            return header + "Пусто.", None

        lines = []
        for i, f in enumerate(files, offset + 1):
            size_kb = f["size_bytes"] / 1024
            if size_kb >= 1024:
                size_str = f"{size_kb / 1024:.1f}MB"
            else:
                size_str = f"{size_kb:.0f}KB"
            name = f["original_name"]
            if len(name) > 35:
                name = name[:32] + "..."
            lines.append(
                f"{i}. {name}\n"
                f"   📁 {f['category']} · {size_str} · {f['created_at'][:10]}"
            )

        text = header + "\n\n".join(lines)

        # Navigation buttons
        cat_data = category or "_"
        buttons = []
        if offset > 0:
            buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"fp:{max(0, offset - self._FILES_PER_PAGE)}:{cat_data}"))
        if has_next:
            buttons.append(InlineKeyboardButton("Вперёд ➡️", callback_data=f"fp:{offset + self._FILES_PER_PAGE}:{cat_data}"))

        markup = InlineKeyboardMarkup([buttons]) if buttons else None
        return text, markup

    @owner_only
    async def cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = " ".join(context.args) if context.args else ""
        if not query:
            await update.message.reply_text("Используй: /search <запрос>")
            return
        await self._do_search(update, query)

    @owner_only
    async def cmd_analytics(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = " ".join(context.args) if context.args else ""
        if not query:
            await update.message.reply_text("Используй: /analytics <вопрос>\n\nПример: /analytics проанализируй гемоглобин за последний год")
            return
        await self._do_analytics(update, query)

    @owner_only
    async def cmd_recent(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        n = 10
        if context.args:
            try:
                n = min(int(context.args[0]), 50)
            except ValueError:
                pass
        files = await self.pipeline.db.list_files(limit=n)
        if not files:
            await update.message.reply_text("📭 Файлов пока нет.")
            return
        lines = []
        for f in files:
            lines.append(
                f"• {f['original_name']}\n"
                f"  📁 {f['category']} · {f['created_at'][:10]}"
            )
        await update.message.reply_text(
            f"📋 Последние {len(files)} файлов:\n\n" + "\n\n".join(lines)
        )

    @owner_only
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        stats = await self.pipeline.db.get_stats()
        llm_stats = self.pipeline.llm.get_stats()
        cats = "\n".join(f"  {k}: {v}" for k, v in stats["categories"].items()) or "  —"
        size_mb = stats["total_size_bytes"] / (1024 * 1024)
        await update.message.reply_text(
            f"📊 Статистика:\n\n"
            f"Файлов: {stats['total_files']}\n"
            f"Размер: {size_mb:.1f} MB\n"
            f"Категории:\n{cats}\n\n"
            f"LLM вызовов: {llm_stats['total_calls']}\n"
            f"Расход: ${llm_stats['total_cost_usd']:.4f}"
        )

    @owner_only
    async def cmd_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show today's note summary with category breakdown."""
        from datetime import datetime
        db = self.pipeline.db
        today = datetime.now().strftime("%Y-%m-%d")
        notes = await db.get_daily_notes(today)

        if not notes:
            # Fall back to recent notes
            notes = await db.list_notes(limit=10)
            if not notes:
                await update.message.reply_text("📝 Заметок пока нет. Отправь голосовое или /note <текст>")
                return
            lines = []
            for n in notes:
                cat_badge = f"[{n.get('category', '')}] " if n.get("category") else ""
                lines.append(f"• {cat_badge}{n.get('title', '') or n['content'][:60]}\n  {n['created_at'][:16]}")
            await update.message.reply_text(f"📝 Последние заметки:\n\n" + "\n\n".join(lines))
            return

        # Today's summary
        cat_emoji = {"food": "🍽", "health": "🏥", "fitness": "💪", "business": "💼",
                     "personal": "💭", "finance": "💰", "learning": "📚", "goals": "🎯"}
        by_cat: dict[str, list[dict]] = {}
        for n in notes:
            cat = n.get("category", "other") or "other"
            by_cat.setdefault(cat, []).append(n)

        lines = [f"📊 Сегодня ({today}) — {len(notes)} заметок:", ""]
        for cat, cat_notes in sorted(by_cat.items()):
            emoji = cat_emoji.get(cat, "📝")
            lines.append(f"{emoji} **{cat}** ({len(cat_notes)})")
            for n in cat_notes[:3]:
                title = n.get("title", "") or n.get("content", "")[:50]
                lines.append(f"  • {title}")
            if len(cat_notes) > 3:
                lines.append(f"  ...и ещё {len(cat_notes) - 3}")

        # Show metrics
        metrics = await db.get_daily_facts(today) if hasattr(db, 'get_daily_facts') else await db.get_daily_metrics(today)
        if metrics:
            lines.append("")
            if "calories" in metrics:
                lines.append(f"🔥 Калории: ~{int(metrics['calories']['total'])} kcal")
            if "mood_score" in metrics:
                lines.append(f"💭 Настроение: {metrics['mood_score']['avg']:.1f}/10")
            if "weight_kg" in metrics:
                lines.append(f"⚖️ Вес: {metrics['weight_kg']['avg']:.1f} кг")

        await update.message.reply_text("\n".join(lines))

    @owner_only
    async def cmd_unlock(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Unlock encryption via Telegram: /unlock <password> [PIN]"""
        from app.utils.crypto import is_encryption_configured, unlock_with_password
        from app.main import _state
        import os

        # PIN check for critical operation
        pin_err = _check_pin(context)
        if pin_err:
            await update.message.reply_text(pin_err)
            return

        if not is_encryption_configured():
            await update.message.reply_text(
                "🔓 Шифрование не настроено.\n"
                "Настройте через Settings → Encryption в веб-панели."
            )
            return

        if _state.get("_encryption_key"):
            await update.message.reply_text("🔓 Уже разблокировано.")
            return

        args = context.args
        if not args:
            await update.message.reply_text(
                "🔐 Использование: /unlock <мастер-пароль>\n\n"
                "⚠️ Пароль будет виден в чате — после разблокировки "
                "удалите сообщение."
            )
            return

        password = " ".join(args)

        # Delete the message with password immediately
        try:
            await update.message.delete()
        except Exception:
            pass

        key_file_data = None
        kf_path = os.environ.get("KEY_FILE", "")
        if kf_path:
            from pathlib import Path as _P
            kf = _P(kf_path)
            if kf.exists():
                key_file_data = kf.read_bytes()

        try:
            key = unlock_with_password(password, "data/encryption.key", key_file_data)
        except ValueError as e:
            await update.message.chat.send_message(f"❌ {e}")
            return

        # Apply key to ALL running services
        _state["_encryption_key"] = key
        db = self.pipeline.db
        if db:
            db._enc_key = key
        fs = self.pipeline.file_storage
        if fs:
            for backend in fs._backends.values():
                if hasattr(backend, "_encryption_key"):
                    backend._encryption_key = key
        vs = self.pipeline.vector_store
        if vs:
            vs._strip_text = True

        await update.message.chat.send_message(
            "🔓 Шифрование разблокировано!\n"
            "Ключ в памяти — при перезапуске потребуется снова."
        )

    @owner_only
    async def cmd_skills(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        skills = self.pipeline.skills.list_skills()
        if not skills:
            await update.message.reply_text("Скиллов пока нет.")
            return
        lines = []
        for s in skills:
            status = "✅" if s.enabled else "⏸"
            lines.append(f"{status} {s.effective_display_name} → 📁 {s.category}")
        await update.message.reply_text("🧩 Скиллы:\n\n" + "\n".join(lines))

    @owner_only
    async def cmd_insights(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show AI insights summary for all categories."""
        ack = await update.message.reply_text("💡 Генерирую обзор...")
        try:
            from app.main import get_state
            insights_engine = get_state("insights_engine")
            db = self.pipeline.db
            if not insights_engine:
                await ack.edit_text("❌ Insights engine не инициализирован.")
                return

            insights = await db.get_all_insights()
            if not insights:
                # Generate fresh
                await insights_engine.refresh_all()
                insights = await db.get_all_insights()

            if not insights:
                await ack.edit_text("📭 Нет данных для анализа.")
                return

            cat_icons = {'personal': '🆔', 'business': '💼', 'health': '🏥'}
            parts = ["💡 AI Insights\n"]
            for ins in insights:
                icon = cat_icons.get(ins['category'], '📁')
                parts.append(f"{icon} {ins['category'].upper()} ({ins['document_count']} docs)")
                if ins['summary_text']:
                    parts.append(ins['summary_text'][:300])
                if ins['key_issues']:
                    parts.append(f"⚠️ {ins['key_issues'][:200]}")
                if ins['recommendations']:
                    parts.append(f"✅ {ins['recommendations'][:300]}")
                parts.append("")

            text = "\n".join(parts)
            # Telegram limit
            if len(text) > 4000:
                text = text[:3997] + "..."
            await ack.edit_text(text)
        except Exception as e:
            await ack.edit_text(f"❌ Ошибка: {e}")

    # ── File Handlers ───────────────────────────────────────────────────

    @owner_only
    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle uploaded documents."""
        _save_chat_id(update.message.chat_id)
        doc = update.message.document
        if doc.file_size and doc.file_size > MAX_TELEGRAM_FILE_SIZE:
            await update.message.reply_text(f"❌ Файл слишком большой (макс 20MB)")
            return

        # Instant ack
        ack = await update.message.reply_text(
            f"⏳ Обрабатываю: {doc.file_name}..."
        )

        # Download
        try:
            tg_file = await doc.get_file()
            buf = BytesIO()
            await tg_file.download_to_memory(buf)
            file_data = buf.getvalue()
        except Exception as e:
            await ack.edit_text(f"❌ Не удалось скачать файл: {e}")
            return

        # Process in background
        task = asyncio.create_task(
            self._process_and_reply(ack, file_data, doc.file_name or "document", context)
        )
        task.add_done_callback(_log_task_exception)

    @owner_only
    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle photo messages — scan session, media group, or single photo."""
        photo = update.message.photo[-1]  # largest size

        # 1. Check if scan session is active
        session = context.user_data.get("scan_session")
        if session is not None:
            try:
                tg_file = await photo.get_file()
                buf = BytesIO()
                await tg_file.download_to_memory(buf)
                session["images"].append(buf.getvalue())
                n = len(session["images"])
                await update.message.reply_text(f"📄 Страница {n} добавлена (всего: {n})")
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка: {e}")
            return

        # 2. Check for media group (multiple photos sent at once)
        if update.message.media_group_id:
            group_id = update.message.media_group_id
            key = f"mg:{group_id}"
            try:
                tg_file = await photo.get_file()
                buf = BytesIO()
                await tg_file.download_to_memory(buf)
            except Exception as e:
                logger.warning(f"Failed to download media group photo: {e}")
                return

            is_new = key not in context.bot_data
            if is_new:
                context.bot_data[key] = {
                    "images": [],
                    "chat_id": update.message.chat_id,
                }
            if len(context.bot_data[key]["images"]) >= 20:
                return  # Limit: max 20 photos per media group
            context.bot_data[key]["images"].append(buf.getvalue())

            if is_new:
                # Schedule processing after 5 seconds (wait for all photos to arrive + download)
                context.job_queue.run_once(
                    self._process_media_group, 5.0,
                    data={"group_id": group_id},
                    name=key,
                    chat_id=update.message.chat_id,
                )
            return

        # 3. Single photo — process as before
        ack = await update.message.reply_text("⏳ Обрабатываю фото...")

        try:
            tg_file = await photo.get_file()
            buf = BytesIO()
            await tg_file.download_to_memory(buf)
            file_data = buf.getvalue()
        except Exception as e:
            await ack.edit_text(f"❌ Не удалось скачать фото: {e}")
            return

        filename = f"photo_{photo.file_unique_id}.jpg"
        task = asyncio.create_task(self._process_and_reply(ack, file_data, filename, context))
        task.add_done_callback(_log_task_exception)

    async def _process_media_group(self, context: ContextTypes.DEFAULT_TYPE):
        """Process collected media group photos — combine into PDF."""
        job = context.job
        group_id = job.data["group_id"]
        key = f"mg:{group_id}"
        group_data = context.bot_data.pop(key, None)
        if not group_data or not group_data["images"]:
            return

        chat_id = group_data["chat_id"]
        n = len(group_data["images"])
        ack = await context.bot.send_message(chat_id, f"⏳ Получено {n} фото, собираю в PDF...")

        try:
            from app.utils.pdf import images_to_pdf
            pdf_bytes = images_to_pdf(group_data["images"])
            from datetime import datetime
            filename = f"scan_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
            task = asyncio.create_task(self._process_and_reply(ack, pdf_bytes, filename, context))
            task.add_done_callback(_log_task_exception)
        except Exception as e:
            logger.error(f"Media group PDF error: {e}", exc_info=True)
            try:
                await ack.edit_text(f"❌ Ошибка сборки PDF: {e}")
            except Exception:
                pass

    @owner_only
    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages — show choice: search or note."""
        text = update.message.text.strip()
        if not text:
            return

        # Store text for callback
        import hashlib
        text_key = hashlib.md5(text.encode()).hexdigest()[:8]
        self._pending_files[f"tc:{text_key}"] = text

        preview = text[:100] + ("..." if len(text) > 100 else "")
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🔍 Вопрос", callback_data=f"tc:s:{text_key}"),
                InlineKeyboardButton("📝 Заметка", callback_data=f"tc:n:{text_key}"),
            ]
        ])
        await update.message.reply_text(f"«{preview}»", reply_markup=keyboard)

    @owner_only
    async def handle_text_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text message choice: search or save as note."""
        query = update.callback_query
        await _safe_answer(query)

        parts = query.data.split(":")  # "tc:s:abcd1234" or "tc:n:abcd1234"
        if len(parts) < 3:
            return

        action = parts[1]
        text_key = parts[2]
        text = self._pending_files.pop(f"tc:{text_key}", None)

        if not text:
            await _safe_edit(query, "❌ Данные устарели. Отправьте сообщение заново.")
            return

        if action == "s":
            await _safe_edit(query, f"«{text[:100]}»\n\n🔍 Ищу...")
            # Create a fake Update to reuse _do_search
            class _FakeUpdate:
                def __init__(self, message, chat):
                    self.message = message
                    self.effective_chat = chat
            class _FakeMessage:
                def __init__(self, reply_fn, chat_id):
                    self.reply_text = reply_fn
                    self.chat_id = chat_id
            chat = query.message.chat
            async def _reply(text_msg, **kwargs):
                return await context.bot.send_message(chat_id=chat.id, text=text_msg, **kwargs)
            fake_update = _FakeUpdate(_FakeMessage(_reply, chat.id), chat)
            await self._do_search(fake_update, text, context)

        elif action == "n":
            await _safe_edit(query, f"«{text[:100]}»\n\n⏳ Сохраняю заметку...")
            await self._save_smart_note(text, query.message.chat_id, query, source="text")

    @owner_only
    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle voice messages — transcribe via Whisper, then search or save as note."""
        voice = update.message.voice

        ack = await update.message.reply_text("🎤 Распознаю голосовое...")

        try:
            # Download OGG
            tg_file = await voice.get_file()
            buf = BytesIO()
            await tg_file.download_to_memory(buf)
            buf.seek(0)
            buf.name = "voice.ogg"

            # Transcribe via Whisper
            from openai import OpenAI
            from app.config import get_settings
            settings = get_settings()

            if not settings.openai_api_key:
                await ack.edit_text("❌ OpenAI API ключ не настроен (нужен для Whisper)")
                return

            import time as _time
            _whisper_start = _time.monotonic()
            client = OpenAI(api_key=settings.openai_api_key)
            transcript = client.audio.transcriptions.create(model="whisper-1", file=buf)
            text = transcript.text.strip()
            _whisper_ms = int((_time.monotonic() - _whisper_start) * 1000)

            # Log Whisper cost: $0.006 per second of audio
            duration_sec = voice.duration or 0
            whisper_cost = duration_sec * 0.006
            try:
                await self.pipeline.db.log_llm_usage(
                    role="whisper", model="whisper-1",
                    input_tokens=duration_sec,  # seconds as "tokens" for tracking
                    output_tokens=len(text.split()),
                    cost_usd=whisper_cost,
                    latency_ms=_whisper_ms,
                )
            except Exception:
                pass

            if not text:
                await ack.edit_text("🎤 Не удалось распознать речь.")
                return

            # Show text + 2 buttons: user decides
            import secrets
            voice_key = secrets.token_hex(4)
            self._pending_files[f"vc:{voice_key}"] = text

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🔍 Поиск", callback_data=f"vc:s:{voice_key}"),
                    InlineKeyboardButton("📝 Заметка", callback_data=f"vc:n:{voice_key}"),
                ]
            ])
            await ack.edit_text(f"🎤 «{text}»", reply_markup=keyboard)

        except Exception as e:
            logger.error(f"Voice processing error: {e}", exc_info=True)
            try:
                await ack.edit_text(f"❌ Ошибка: {e}")
            except Exception:
                pass


    # ── Dedup Callback ──────────────────────────────────────────────────

    @owner_only
    async def handle_voice_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle voice message choice: search or save as note."""
        query = update.callback_query
        await _safe_answer(query)

        parts = query.data.split(":")  # "vc:s:abcd1234" or "vc:n:abcd1234"
        if len(parts) < 3:
            return

        action = parts[1]
        voice_key = parts[2]
        text = self._pending_files.pop(f"vc:{voice_key}", None)

        if not text:
            await _safe_edit(query, "❌ Данные устарели. Отправьте голосовое заново.")
            return

        if action == "s":
            # Search
            await _safe_edit(query, f"🎤 «{text}»\n\n🔍 Ищу...")
            # Create a fake update-like object to reply in the same chat
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text="⏳ Обрабатываю запрос..."
            )
            # Run search
            if self.search_fn:
                try:
                    history = context.user_data.get("search_history", []) if context else None
                    result = await self.search_fn(text, history=history)
                    answer = result["text"] if isinstance(result, dict) else result
                    file_ids = result.get("file_ids", {}) if isinstance(result, dict) else {}

                    keyboard = []
                    for fid, fname in file_ids.items():
                        short_key = fid[:8]
                        self._pending_files[f"fs:{short_key}"] = fid
                        keyboard.append(
                            InlineKeyboardButton("📎 Скачать документ", callback_data=f"file:s:{short_key}")
                        )

                    reply_markup = InlineKeyboardMarkup([keyboard]) if keyboard else None
                    await context.bot.send_message(
                        chat_id=query.message.chat_id,
                        text=answer,
                        reply_markup=reply_markup,
                    )

                    if context and hasattr(context, 'user_data'):
                        hist = context.user_data.setdefault("search_history", [])
                        hist.append({"q": text, "a": answer[:300]})
                        if len(hist) > 5:
                            context.user_data["search_history"] = hist[-5:]
                except Exception as e:
                    await context.bot.send_message(chat_id=query.message.chat_id, text=f"❌ Ошибка: {e}")

        elif action == "n":
            await _safe_edit(query, f"🎤 «{text}»\n\n⏳ Обрабатываю заметку...")
            await self._save_smart_note(text, query.message.chat_id, query)

    async def _save_smart_note(self, text: str, chat_id: int, callback_query, source: str = "voice"):
        """Capture note instantly, enqueue async enrichment. Never blocks on LLM."""
        from app.main import get_state

        capture = get_state("note_capture")
        if capture:
            note_id = await capture.capture(text, source=source)
            await _safe_edit(callback_query, f"📝 Сохранено (#{note_id}). Обработка в фоне...")
        else:
            # Fallback: direct DB save
            db = self.pipeline.db
            note_id = await db.save_note(content=text, source=source)
            await _safe_edit(callback_query, f"📝 Сохранено (#{note_id})")

    @owner_only
    async def cmd_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Quick text note via /note command — instant capture."""
        text = " ".join(context.args) if context.args else ""
        if not text:
            await update.message.reply_text("Использование: /note <текст заметки>")
            return

        from app.main import get_state
        capture = get_state("note_capture")
        if capture:
            note_id = await capture.capture(text, source="command")
            await update.message.reply_text(f"📝 Сохранено (#{note_id}). Обработка в фоне...")
        else:
            note_id = await self.pipeline.db.save_note(content=text, source="command")
            await update.message.reply_text(f"📝 Сохранено (#{note_id})")

    @owner_only
    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show today's metrics summary."""
        from datetime import datetime
        db = self.pipeline.db
        today = datetime.now().strftime("%Y-%m-%d")

        notes = await db.get_daily_notes(today)
        metrics = await db.get_daily_facts(today) if hasattr(db, 'get_daily_facts') else await db.get_daily_metrics(today)
        streak = await db.get_streak()

        # Count by category
        by_cat: dict[str, int] = {}
        for n in notes:
            cat = n.get("category", "other") or "other"
            by_cat[cat] = by_cat.get(cat, 0) + 1
        cat_str = ", ".join(f"{k}:{v}" for k, v in sorted(by_cat.items())) if by_cat else "нет"

        parts = [f"📊 Сегодня ({today}):", ""]
        # Calories with meal breakdown
        if metrics.get("calories"):
            parts.append(f"🍽 Калории: ~{int(metrics['calories']['total'])} kcal")
        else:
            parts.append("🍽 Калории: — (нет данных)")
        # Sleep
        if metrics.get("sleep_hours"):
            sleep_str = f"😴 Сон: {metrics['sleep_hours']['total']:.1f}ч"
            if metrics.get("sleep_quality"):
                sleep_str += f" (качество: {metrics['sleep_quality']['avg']:.0f}/10)"
            parts.append(sleep_str)
        # Mood & Energy
        mood_str = ""
        if metrics.get("mood_score"):
            mood_str += f"💭 Настроение: {metrics['mood_score']['avg']:.1f}/10"
        if metrics.get("energy"):
            mood_str += f" | Энергия: {metrics['energy']['avg']:.0f}/10"
        if mood_str:
            parts.append(mood_str)
        # Weight
        if metrics.get("weight_kg"):
            parts.append(f"⚖️ Вес: {metrics['weight_kg']['avg']:.1f} кг")
        # Notes
        parts.append(f"📝 Заметок: {len(notes)} ({cat_str})")
        # Streak
        if streak > 1:
            parts.append(f"🔥 Стрик: {streak} дней подряд!")

        await update.message.reply_text("\n".join(parts))

    @owner_only
    async def cmd_missing(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show what data is missing for today."""
        from datetime import datetime
        db = self.pipeline.db
        today = datetime.now().strftime("%Y-%m-%d")

        metrics = await db.get_daily_facts(today) if hasattr(db, 'get_daily_facts') else await db.get_daily_metrics(today)
        notes = await db.get_daily_notes(today)
        categories_today = set(n.get("category", "") for n in notes)

        parts = [f"📋 Данные за сегодня ({today}):", ""]

        checks = [
            ("food" in categories_today, "Еда", metrics.get("calories")),
            (metrics.get("mood_score") is not None, "Настроение", metrics.get("mood_score")),
            (metrics.get("sleep_hours") is not None, "Сон", metrics.get("sleep_hours")),
            (metrics.get("weight_kg") is not None, "Вес", metrics.get("weight_kg")),
            (metrics.get("energy") is not None, "Энергия", metrics.get("energy")),
        ]

        for filled, label, data in checks:
            if filled and data:
                if label == "Еда":
                    parts.append(f"✅ {label} — {int(data['total'])} kcal")
                elif label in ("Настроение", "Энергия"):
                    parts.append(f"✅ {label} — {data['avg']:.1f}/10")
                elif label == "Сон":
                    parts.append(f"✅ {label} — {data['total']:.1f}ч")
                elif label == "Вес":
                    parts.append(f"✅ {label} — {data['avg']:.1f} кг")
            else:
                parts.append(f"❌ {label} — не записано")

        await update.message.reply_text("\n".join(parts))

    @owner_only
    async def cmd_morning(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send morning briefing on demand."""
        ack = await update.message.reply_text("☀️ Генерирую утренний брифинг...")

        from app.main import get_state
        morning_engine = get_state("morning_engine")
        if not morning_engine:
            await ack.edit_text("❌ Morning engine не инициализирован")
            return

        try:
            brief = await morning_engine.generate_morning_brief()
            text = morning_engine.format_telegram_brief(brief)
            await ack.edit_text(text or "Недостаточно данных для брифинга")
        except Exception as e:
            await ack.edit_text(f"❌ Ошибка: {e}")

    @owner_only
    async def cmd_habits(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show today's habit status and streaks."""
        from datetime import datetime
        from app.notes.habits import HabitTracker
        tracker = HabitTracker(self.pipeline.db)
        today = datetime.now().strftime("%Y-%m-%d")
        statuses = await tracker.check_habits_for_date(today)

        if not statuses:
            await update.message.reply_text("Привычки не настроены. Создайте на /notes/habits")
            return

        lines = [f"📋 Привычки ({today}):", ""]
        for h in statuses:
            check = "✅" if h["completed"] else "⬜"
            streak_str = f" 🔥{h['streak']}" if h["streak"] > 0 else ""
            lines.append(f"{check} {h['name']}{streak_str}")

        done = sum(1 for h in statuses if h["completed"])
        lines.append(f"\n{done}/{len(statuses)} выполнено")

        await update.message.reply_text("\n".join(lines))

    @owner_only
    async def cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Export notes data as JSON file."""
        ack = await update.message.reply_text("📦 Готовлю экспорт...")

        try:
            from app.notes.export import ExportService
            svc = ExportService(self.pipeline.db)
            data = await svc.export_all_json()

            from io import BytesIO
            buf = BytesIO(data.encode("utf-8"))
            buf.name = "notes_export.json"
            buf.seek(0)

            from datetime import datetime
            now = datetime.now().strftime("%Y%m%d")
            await update.message.reply_document(
                document=buf,
                filename=f"notes_export_{now}.json",
                caption=f"📦 Экспорт заметок (последние 30 дней)",
            )
            await ack.delete()
        except Exception as e:
            await ack.edit_text(f"❌ Ошибка экспорта: {e}")

    @owner_only
    async def cmd_analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Run weekly analysis on demand."""
        ack = await update.message.reply_text("📊 Генерирую недельный отчёт...")

        from app.main import get_state
        note_agent = get_state("note_agent")
        if not note_agent:
            await ack.edit_text("❌ Note Agent не инициализирован")
            return

        try:
            from app.notes.weekly import WeeklyAnalyzer
            analyzer = WeeklyAnalyzer(
                self.pipeline.db, self.pipeline.llm,
                vault=note_agent.vault,
            )
            summary = await analyzer.get_weekly_telegram_summary()
            await ack.edit_text(summary or "Недостаточно данных для анализа")
        except Exception as e:
            await ack.edit_text(f"❌ Ошибка: {e}")

    @owner_only
    async def handle_note_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle note action buttons: note:ok:ID, note:edit:ID, note:archive:ID."""
        query = update.callback_query
        await _safe_answer(query)

        parts = query.data.split(":")
        if len(parts) < 3:
            return

        action = parts[1]
        try:
            note_id = int(parts[2])
        except ValueError:
            return

        db = self.pipeline.db

        if action == "ok":
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        elif action == "archive":
            await db.set_note_status(note_id, "archived")
            await _safe_edit(query, f"📦 Заметка #{note_id} архивирована")
        elif action == "pin":
            is_pinned = await db.toggle_note_pin(note_id)
            emoji = "📌" if is_pinned else "📎"
            label = "закреплена" if is_pinned else "откреплена"
            await _safe_answer(query, f"{emoji} Заметка #{note_id} {label}", show_alert=False)
        elif action == "edit":
            # v1: Edit metadata only — redirect to web detail page
            from app.config import get_settings
            s = get_settings()
            host = s.web.host if s.web.host != "0.0.0.0" else "localhost"
            url = f"http://{host}:{s.web.port}/notes/{note_id}"
            await _safe_edit(query, f"✏️ Редактировать заметку #{note_id}:\n{url}")

    @owner_only
    async def handle_checkin_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle evening check-in callbacks: ci:score:N:cat, ci:skip:cat, ci:done."""
        query = update.callback_query
        await _safe_answer(query)

        parts = query.data.split(":")
        if len(parts) < 2:
            return

        from app.main import get_state

        from app.notes.checkin import EveningCheckin
        from app.config import get_settings
        ns = get_settings().notes
        checkin = EveningCheckin(
            self.pipeline.db,
            expected_categories=ns.expected_daily_categories,
            expected_signals=ns.expected_daily_signals,
            capture=get_state("note_capture"),
            max_questions=ns.checkin_max_questions,
            include_closing=ns.checkin_include_closing_prompt,
            weight_frequency_days=ns.checkin_weight_frequency_days,
        )

        action = parts[1]
        tg_app = get_state("tg_app")
        chat_id = query.message.chat_id

        if action == "score" and len(parts) >= 4:
            score = int(parts[2])
            category = parts[3]
            await _safe_edit(query, f"💭 Настроение: {score}/10 ✓")
            await checkin.handle_mood_score(score, chat_id, tg_app, category)
        elif action == "skip" and len(parts) >= 3:
            category = parts[2]
            await _safe_edit(query, "⏭ Пропущено")
            await checkin.handle_skip(chat_id, tg_app, category)
        elif action == "done":
            await _safe_edit(query, "✅ Check-in завершён")
            await checkin.handle_done(chat_id, tg_app)

    @owner_only
    async def handle_reminder_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle reminder buttons: done or snooze."""
        query = update.callback_query
        await _safe_answer(query)

        parts = query.data.split(":")  # "rem:done:123" or "rem:snooze:123"
        if len(parts) < 3:
            return

        action = parts[1]
        reminder_id = int(parts[2])
        db = self.pipeline.db

        if action == "done":
            await db.mark_reminder_sent(reminder_id)
            await _safe_edit(query, query.message.text + "\n\n✅ Отмечено как выполненное.")
        elif action == "snooze":
            from datetime import datetime, timedelta
            new_date = (datetime.now() + timedelta(days=1)).isoformat()
            await db.db.execute(
                "UPDATE reminders SET remind_at=?, sent=0 WHERE id=?", (new_date, reminder_id)
            )
            await db.db.commit()
            await _safe_edit(query, query.message.text + "\n\n⏰ Отложено на 1 день.")

    @owner_only
    async def handle_note_reminder_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle note reminder buttons: create, done, snooze."""
        query = update.callback_query
        await _safe_answer(query)

        parts = query.data.split(":")  # "nrem:create:14" or "nrem:done:5"
        if len(parts) < 3:
            return

        action = parts[1]
        id_val = int(parts[2])
        db = self.pipeline.db

        if action == "create":
            from app.notes.reminders import ReminderExtractionService
            svc = ReminderExtractionService(db)
            count = await svc.create_for_inferred(id_val)  # id_val = note_id
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            if count:
                await query.message.reply_text(f"⏰ Создано напоминаний: {count}")
            else:
                await query.message.reply_text("Нет задач с дедлайном для напоминания")
        elif action == "done":
            await db.complete_note_reminder(id_val)  # id_val = reminder_id
            await _safe_edit(query, query.message.text + "\n\n✅ Выполнено")
        elif action == "snooze":
            await db.snooze_note_reminder(id_val)  # id_val = reminder_id
            await _safe_edit(query, query.message.text + "\n\n⏰ Отложено на 24ч")

    @owner_only
    async def handle_dedup_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user's choice on semantic duplicate: n=keep_new / o=keep_old / b=keep_both."""
        query = update.callback_query
        await _safe_answer(query)

        data = query.data  # "dedup:n:abcd1234" etc.
        parts = data.split(":")
        if len(parts) < 3:
            await _safe_edit(query, "❌ Неверные данные")
            return

        action = parts[1]
        dedup_key = parts[2]

        # Retrieve full IDs from bot_data
        ids = context.bot_data.pop(f"dd:{dedup_key}", None)
        if not ids:
            await _safe_edit(query, "❌ Данные устарели. Обработайте файл заново.")
            return

        new_file_id = ids["new"]
        old_file_id = ids["old"]

        if action == "n":
            await self._cascade_delete(old_file_id)
            await _safe_edit(query, "✅ Оставлен новый файл. Старый удалён.")
        elif action == "o":
            await self._cascade_delete(new_file_id)
            await _safe_edit(query, "✅ Оставлен старый файл. Новый удалён.")
        elif action == "b":
            await _safe_edit(query, "✅ Оба файла сохранены.")
        else:
            await _safe_edit(query, "❌ Неизвестное действие")

    @owner_only
    async def handle_file_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a file document to the user when they click a file button."""
        query = update.callback_query
        await _safe_answer(query)

        parts = query.data.split(":")  # "file:s:short_key"
        if len(parts) < 3:
            return

        short_key = parts[2]
        file_id = self._pending_files.pop(f"fs:{short_key}", None) or context.bot_data.pop(f"fs:{short_key}", None)
        if not file_id:
            await _safe_answer(query, "❌ Данные устарели", show_alert=True)
            return

        db = self.pipeline.db
        file = await db.get_file(file_id)
        if not file or not file.get("stored_path"):
            await _safe_answer(query, "❌ Файл не найден", show_alert=True)
            return

        stored_uri = file["stored_path"]
        if not await self.pipeline.file_storage.exists(stored_uri):
            await _safe_answer(query, "❌ Файл удалён", show_alert=True)
            return

        import io
        data = await self.pipeline.file_storage.read_file(stored_uri)
        await context.bot.send_document(
            chat_id=query.message.chat_id,
            document=io.BytesIO(data),
            filename=file.get("original_name", "file"),
        )

    async def _cascade_delete(self, file_id: str):
        """Delete file via lifecycle service (disk + Qdrant + SQLite + cache)."""
        from app.main import get_state
        lifecycle = get_state("lifecycle")
        if lifecycle:
            await lifecycle.delete(file_id)
        else:
            # Fallback if lifecycle not initialized
            db = self.pipeline.db
            file = await db.get_file(file_id)
            if file and file.get("stored_path"):
                try:
                    await self.pipeline.file_storage.delete(file["stored_path"])
                except Exception:
                    pass
            try:
                await self.pipeline.vector_store.delete_document(file_id)
            except Exception:
                pass
            await db.delete_file(file_id)
            logger.info(f"Cascade deleted file {file_id} (fallback)")

    # ── Helpers ─────────────────────────────────────────────────────────

    def _is_encryption_locked(self) -> bool:
        """Check if encryption is configured but not yet unlocked."""
        from app.utils.crypto import is_encryption_configured
        from app.main import _state
        from app.config import get_settings
        s = get_settings()
        if not (s.encryption.files or s.encryption.database):
            return False
        return is_encryption_configured() and _state.get("_encryption_key") is None

    async def _process_and_reply(self, ack_message, file_data: bytes, filename: str, context: ContextTypes.DEFAULT_TYPE):
        """Run pipeline and edit the ack message with results."""
        try:
            # Warn if encryption is locked
            if self._is_encryption_locked():
                await ack_message.edit_text(
                    "🔒 Шифрование заблокировано!\n\n"
                    "Файл НЕ будет зашифрован. Разблокируйте:\n"
                    "• Коман��а /unlock <пароль>\n"
                    "• Или через Settings → Encryption в веб-панели"
                )
                return

            result = await self.pipeline.process(file_data, filename)

            # If semantic duplicate found — show inline keyboard
            if result.semantic_duplicate_of and result.file_id:
                sd = result.semantic_duplicate_of
                new_id = result.file_id
                old_id = sd.get("id", "")
                score_pct = f"{result.similarity_score:.0%}"

                # Store full IDs in bot_data with short key (Telegram limits callback_data to 64 bytes)
                dedup_key = new_id[:8]
                context.bot_data[f"dd:{dedup_key}"] = {"new": new_id, "old": old_id}

                text = (
                    f"{result.summary_text()}\n\n"
                    f"⚠️ Похожий документ найден ({score_pct}):\n"
                    f"📄 {sd.get('original_name', '?')}\n"
                    f"📅 {sd.get('created_at', '?')[:16]}\n\n"
                    f"Что делать?"
                )

                keyboard = [
                    [
                        InlineKeyboardButton("🆕 Оставить новый", callback_data=f"dedup:n:{dedup_key}"),
                        InlineKeyboardButton("📂 Оставить старый", callback_data=f"dedup:o:{dedup_key}"),
                    ],
                    [
                        InlineKeyboardButton("📎 Оставить оба", callback_data=f"dedup:b:{dedup_key}"),
                    ],
                ]
                await ack_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
                self._schedule_delete(ack_message, context)
            else:
                await ack_message.edit_text(result.summary_text())
                self._schedule_delete(ack_message, context)
        except Exception as e:
            logger.error(f"Pipeline error for {filename}: {e}", exc_info=True)
            try:
                await ack_message.edit_text(f"❌ Ошибка обработки: {e}")
            except Exception:
                pass

    async def _do_analytics(self, update: Update, query: str):
        """Perform multi-document analytics and reply with chart."""
        await update.effective_chat.send_action("typing")
        ack = await update.message.reply_text("📊 Анализирую документы...")

        try:
            await update.effective_chat.send_action("typing")
            result = await self.analytics_fn(query)

            # Send text summary
            await ack.edit_text(result.text_summary)

            # Send chart as photo if available
            if result.chart_png:
                await update.message.reply_photo(
                    photo=BytesIO(result.chart_png),
                    caption="📈 График по запросу",
                )

            # Build file download buttons
            if result.file_ids:
                keyboard = []
                for fid, fname in result.file_ids.items():
                    short_key = fid[:8]
                    self._pending_files[f"fs:{short_key}"] = fid
                    keyboard.append(
                        InlineKeyboardButton(
                            f"📎 {fname[:30]}", callback_data=f"file:s:{short_key}"
                        )
                    )
                if keyboard:
                    rows = [keyboard[i:i + 2] for i in range(0, len(keyboard), 2)]
                    await update.message.reply_text(
                        f"📄 Исходные документы ({len(result.file_ids)}):",
                        reply_markup=InlineKeyboardMarkup(rows),
                    )

        except Exception as e:
            logger.error(f"Analytics error: {e}", exc_info=True)
            try:
                await ack.edit_text(f"❌ Ошибка анализа: {e}")
            except Exception:
                pass

    async def _do_search(self, update: Update, query: str, context=None):
        """Perform semantic search and reply with file download buttons."""
        chat_id = update.effective_chat.id if update.effective_chat else 0

        # Load history from SQLite (persistent between restarts)
        history = None
        try:
            db = self.pipeline.db
            rows = await db.get_chat_history(chat_id, limit=10)
            if rows:
                history = [{"q": r["content"], "a": ""} if r["role"] == "user" else {"q": "", "a": r["content"]} for r in rows]
                # Merge into Q/A pairs
                pairs = []
                for i in range(0, len(rows) - 1, 2):
                    if rows[i]["role"] == "user" and i + 1 < len(rows):
                        pairs.append({"q": rows[i]["content"], "a": rows[i + 1]["content"][:200]})
                history = pairs[-5:] if pairs else None
        except Exception:
            pass

        # Save user query to history
        try:
            await self.pipeline.db.save_chat_message(chat_id, "user", query)
        except Exception:
            pass

        if self.search_fn:
            try:
                # Show typing indicator while searching
                await update.effective_chat.send_action("typing")
                result = await self.search_fn(query, history=history, compact=True)
                text = result["text"] if isinstance(result, dict) else result
                file_ids = result.get("file_ids", {}) if isinstance(result, dict) else {}

                # Build inline keyboard with file buttons
                keyboard = []
                for fid, fname in file_ids.items():
                    short_key = fid[:8]
                    self._pending_files[f"fs:{short_key}"] = fid
                    # Show filename, trim to fit Telegram button limit
                    label = fname[:40] if len(fname) <= 40 else fname[:37] + "..."
                    keyboard.append(
                        InlineKeyboardButton(f"📎 {label}", callback_data=f"file:s:{short_key}")
                    )

                reply_markup = None
                if keyboard:
                    # One button per row for better filename readability
                    rows = [[btn] for btn in keyboard]
                    reply_markup = InlineKeyboardMarkup(rows)

                # Split long messages (Telegram limit: 4096 chars)
                MAX_TG = 4000
                async def _send(txt, markup=None):
                    try:
                        msg = await update.message.reply_text(txt, reply_markup=markup, parse_mode="HTML")
                    except Exception:
                        msg = await update.message.reply_text(txt, reply_markup=markup)
                    self._schedule_delete(msg)
                    return msg

                if len(text) <= MAX_TG:
                    await _send(text, reply_markup)
                else:
                    parts = []
                    while text:
                        if len(text) <= MAX_TG:
                            parts.append(text)
                            break
                        split_at = text.rfind('\n', 0, MAX_TG)
                        if split_at < MAX_TG // 2:
                            split_at = text.rfind(' ', 0, MAX_TG)
                        if split_at < 1:
                            split_at = MAX_TG
                        parts.append(text[:split_at])
                        text = text[split_at:].lstrip()
                    for i, part in enumerate(parts):
                        markup = reply_markup if i == len(parts) - 1 else None
                        await _send(part, markup)

                # Save bot answer to persistent history
                try:
                    # Link answer to file if found
                    answer_file_id = list(file_ids.keys())[0] if file_ids else ""
                    await self.pipeline.db.save_chat_message(chat_id, "assistant", text[:500], file_id=answer_file_id)
                except Exception:
                    pass

            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка поиска: {e}")
        else:
            try:
                results = await self.pipeline.vector_store.search(query, top_k=3)
                if not results:
                    await update.message.reply_text("🔍 Ничего не найдено.")
                    return
                lines = []
                for i, r in enumerate(results, 1):
                    preview = r.text[:200] + "..." if len(r.text) > 200 else r.text
                    lines.append(f"{i}. {r.metadata.get('filename', '?')}\n{preview}")
                await update.message.reply_text(
                    f"🔍 Результаты по «{query}»:\n\n" + "\n\n".join(lines)
                )
            except Exception as e:
                await update.message.reply_text(f"❌ Ошибка: {e}")
