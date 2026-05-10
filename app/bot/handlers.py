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


def owner_only(func):
    """Restrict handler to the configured owner_id only."""
    @wraps(func)
    async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        owner_id = get_settings().telegram.owner_id
        user_id = update.effective_user.id if update.effective_user else 0
        if not owner_id or user_id != owner_id:
            if update.callback_query:
                await update.callback_query.answer("Бот приватный.", show_alert=True)
            elif update.message:
                await update.message.reply_text("🔒 Бот приватный.")
            return
        return await func(self, update, context)
    return wrapper

_CHAT_ID_FILE = Path("data/chat_id.txt")


def _save_chat_id(chat_id: int):
    _CHAT_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CHAT_ID_FILE.write_text(str(chat_id))


def get_owner_chat_id() -> int | None:
    if _CHAT_ID_FILE.exists():
        try:
            return int(_CHAT_ID_FILE.read_text().strip())
        except ValueError:
            return None
    return None

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

        # File handlers (documents, photos)
        app.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        app.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))

        # Voice messages
        app.add_handler(MessageHandler(filters.VOICE, self.handle_voice))

        # Callback handlers
        app.add_handler(CallbackQueryHandler(self.handle_scan_confirm, pattern="^scan:"))
        app.add_handler(CallbackQueryHandler(self.handle_files_page, pattern="^fp:"))
        app.add_handler(CallbackQueryHandler(self.handle_voice_choice, pattern="^vc:"))
        app.add_handler(CallbackQueryHandler(self.handle_reminder_action, pattern="^rem:"))
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
        await query.answer()
        action = query.data.split(":")[1]  # "confirm" or "cancel"

        session = context.user_data.pop("scan_session", None)

        if action == "cancel":
            if session:
                await query.edit_message_caption(
                    f"🚫 Сканирование «{session['name']}» отменено."
                )
            else:
                try:
                    await query.edit_message_text("🚫 Отменено.")
                except Exception:
                    await query.edit_message_caption("🚫 Отменено.")
            return

        if not session or not session["images"]:
            try:
                await query.edit_message_text("📭 Данные устарели. Начните /scan заново.")
            except Exception:
                await query.edit_message_caption("📭 Данные устарели.")
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
        await query.answer()
        # fp:<offset>:<category or _>
        parts = query.data.split(":", 2)
        offset = int(parts[1]) if len(parts) > 1 else 0
        category = parts[2] if len(parts) > 2 and parts[2] != "_" else None
        text, markup = await self._files_page(offset, category)
        await query.edit_message_text(text, reply_markup=markup)

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
        notes = await self.pipeline.db.list_notes(limit=10)
        if not notes:
            await update.message.reply_text("📝 Заметок пока нет.")
            return
        lines = []
        for n in notes:
            file_ref = f" (📎 к файлу)" if n.get("file_id") else ""
            lines.append(f"• {n['content'][:80]}{file_ref}\n  {n['created_at'][:16]}")
        await update.message.reply_text(f"📝 Заметки ({len(notes)}):\n\n" + "\n\n".join(lines))

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
        """Handle text messages — PIN entry, then analytics or semantic search."""
        query = update.message.text.strip()
        if not query:
            return

        # PIN entry path — if a sensitive file is awaiting unlock, treat
        # the next text message as the PIN attempt.
        pending = context.user_data.get("pending_open")
        if pending:
            await self._handle_pin_attempt(update, context, query, pending)
            return

        # All free text goes to search. Use /analytics for analytics.
        await self._do_search(update, query, context)

    async def _handle_pin_attempt(self, update, context, pin_attempt, pending):
        """Verify PIN, decrypt + send file on success, rate-limit on failure."""
        from app.utils.crypto import verify_pin

        # Lockout check.
        locked_until = context.user_data.get("pin_locked_until", 0)
        if time.time() < locked_until:
            wait_min = int((locked_until - time.time()) / 60) + 1
            await update.message.reply_text(
                f"🚫 Слишком много неверных попыток. Подожди ~{wait_min} мин."
            )
            return

        # Stale prompt (older than 5 min) — drop it.
        if time.time() - pending.get("asked_at", 0) > 300:
            context.user_data.pop("pending_open", None)
            await update.message.reply_text("⏱ Запрос устарел. Нажми кнопку файла ещё раз.")
            return

        db = self.pipeline.db
        pin_hash = await db.get_secret("PIN_HASH") if db else None
        if not pin_hash:
            context.user_data.pop("pending_open", None)
            await update.message.reply_text("🔒 PIN не задан. Установи в /settings.")
            return

        if not verify_pin(pin_attempt, pin_hash):
            pending["attempts"] = pending.get("attempts", 0) + 1
            if pending["attempts"] >= 3:
                # Block for 1 hour.
                context.user_data["pin_locked_until"] = time.time() + 3600
                context.user_data.pop("pending_open", None)
                await update.message.reply_text(
                    "🚫 3 неверных попытки. Открытие заблокировано на 1 час."
                )
            else:
                left = 3 - pending["attempts"]
                await update.message.reply_text(
                    f"❌ Неверный PIN. Осталось попыток: {left}."
                )
            # Best-effort: try to delete the user's PIN message so it doesn't linger.
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        # Success — clear state, decrypt and send.
        file_id = pending["file_id"]
        context.user_data.pop("pending_open", None)
        try:
            await update.message.delete()
        except Exception:
            pass

        file = await db.get_file(file_id)
        if not file or not file.get("stored_path") or not Path(file["stored_path"]).exists():
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="❌ Файл больше недоступен.",
            )
            return
        await self._send_file_to_user(
            context, update.effective_chat.id, file, decrypt=True
        )

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

            # Search-intent shortcut: when the user clearly said "найди X" /
            # "поищи X" / "find X" / "where is X", skip the "Search vs Note"
            # menu and go straight to search. Buttons only appear for
            # ambiguous transcripts (e.g. dictating an actual note).
            t_low = text.strip().lower()
            search_triggers = (
                "найди", "найти", "поищи", "поиск", "ищи", "найдешь",
                "найдёшь", "покажи", "где мой", "где мои", "где моя",
                "find ", "search ", "show me", "where is", "where are",
            )
            search_intent = (
                t_low.startswith(search_triggers)
                or t_low.startswith("?") or t_low.endswith("?")
            )
            if search_intent:
                await ack.edit_text(f"🎤 «{text}»\n🔍 Ищу…")
                await self._do_search(update, text, context)
                return

            # Ambiguous → ask user which path: search or save as note.
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
        await query.answer()

        parts = query.data.split(":")  # "vc:s:abcd1234" or "vc:n:abcd1234"
        if len(parts) < 3:
            return

        action = parts[1]
        voice_key = parts[2]
        text = self._pending_files.pop(f"vc:{voice_key}", None)

        if not text:
            await query.edit_message_text("❌ Данные устарели. Отправьте голосовое заново.")
            return

        if action == "s":
            # Search
            await query.edit_message_text(f"🎤 «{text}»\n\n🔍 Ищу...")
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
            await query.edit_message_text(f"🎤 «{text}»\n\n⏳ Обрабатываю заметку...")
            await self._save_smart_note(text, query.message.chat_id, query)

    async def _save_smart_note(self, text: str, chat_id: int, callback_query):
        """Process note with LLM, find links, save as Obsidian .md."""
        import json as json_mod
        import re
        from datetime import datetime

        db = self.pipeline.db
        llm = self.pipeline.llm

        # 1. LLM: extract title, tags, action items
        note_data = {"title": text[:50], "summary": text, "tags": [], "action_items": []}
        try:
            response = await llm.extract(
                text=text,
                system=(
                    "Обработай голосовую заметку. Ответь ТОЛЬКО JSON:\n"
                    '{"title": "краткий заголовок на русском (3-5 слов)", '
                    '"summary": "суть в 1-2 предложениях", '
                    '"tags": ["тег1", "тег2"], '
                    '"action_items": ["задача если есть"]}'
                ),
            )
            parsed = response.text.strip()
            if parsed.startswith("```"):
                parsed = parsed.split("\n", 1)[1].rsplit("```", 1)[0]
            note_data = json_mod.loads(parsed)
        except Exception as e:
            logger.debug(f"Note LLM extraction failed: {e}")

        title = note_data.get("title", text[:50])
        summary = note_data.get("summary", text)
        tags = note_data.get("tags", [])
        action_items = note_data.get("action_items", [])

        # 2. Find linked file from chat history.
        #
        # We only link when the USER explicitly attached a file in the recent
        # conversation. Previously this looked at any chat_history row with a
        # file_id, which included assistant search responses — so a voice
        # note about dinner ended up "📎 social_security_card.jpg" just
        # because the previous unrelated search had surfaced that file.
        # Today only user-role messages with file_id should count, and the
        # bot doesn't actually populate user-role file_id yet, so this is
        # effectively a no-op until upload events get wired into chat_history.
        linked_file_id = ""
        linked_file_name = ""
        try:
            history = await db.get_chat_history(chat_id, limit=5)
            for h in reversed(history):
                if h.get("role") == "user" and h.get("file_id"):
                    linked_file_id = h["file_id"]
                    f = await db.get_file(linked_file_id)
                    if f:
                        linked_file_name = f.get("original_name", "")
                    break
        except Exception:
            pass

        # 3. Find related notes by tag overlap
        related_notes = []
        try:
            from app.config import get_settings
            notes_dir = get_settings().storage.resolved_path / "notes"
            if notes_dir.exists():
                for md_file in notes_dir.glob("*.md"):
                    content = md_file.read_text()
                    # Check if any tag appears in existing note
                    for tag in tags:
                        if tag.lower() in content.lower():
                            related_notes.append(md_file.stem)
                            break
        except Exception:
            pass

        # 4. Generate .md file
        now = datetime.now()
        slug = re.sub(r'[^\w\s-]', '', title.lower()).strip()
        slug = re.sub(r'[\s]+', '-', slug)[:40]
        filename = f"{now.strftime('%Y-%m-%d')}_{slug}.md"

        from app.config import get_settings
        notes_dir = get_settings().storage.resolved_path / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        md_path = notes_dir / filename

        # Build Obsidian markdown
        lines = [
            "---",
            f"date: {now.strftime('%Y-%m-%d')}",
            f"source: voice",
            f"tags: [{', '.join(tags)}]",
        ]
        if linked_file_id:
            lines.append(f"linked_files: [{linked_file_id}]")
        lines.extend(["---", "", f"# {title}", "", summary, ""])

        if text != summary:
            lines.extend(["## Оригинал", "", text, ""])

        if action_items:
            lines.append("## Задачи")
            for item in action_items:
                lines.append(f"- [ ] {item}")
            lines.append("")

        if related_notes or linked_file_name:
            lines.append("## Связи")
            for rn in related_notes[:5]:
                lines.append(f"- [[{rn}]]")
            if linked_file_name:
                lines.append(f"- Документ: {linked_file_name}")
            lines.append("")

        md_path.write_text("\n".join(lines), encoding="utf-8")

        # 5. Save to SQLite
        note_id = await db.save_note(
            content=summary,
            title=title,
            file_id=linked_file_id,
            md_path=str(md_path),
            source="voice",
            tags=json_mod.dumps(tags),
        )

        # 5b. Ingest into cognee personal memory (non-fatal if sidecar is down).
        try:
            from app.ingestion import ingest_text_to_cognee
            from app.main import get_state
            await ingest_text_to_cognee(
                get_state("cognee"),
                content=text,  # raw transcription, not the LLM-shortened summary
                source_type="note",
                source_id=str(note_id) if note_id else "",
                filename=f"voice_note_{now.strftime('%Y%m%d_%H%M%S')}.txt",
            )
        except Exception as e:
            logger.debug(f"cognee note ingest skipped: {e}")

        # 6. Reply
        reply_parts = [f"📝 **{title}**", "", summary]
        if tags:
            reply_parts.append(f"🏷 {', '.join(tags)}")
        if related_notes:
            reply_parts.append(f"🔗 Связи: {', '.join(related_notes[:3])}")
        if linked_file_name:
            reply_parts.append(f"📎 Документ: {linked_file_name}")
        if action_items:
            reply_parts.append("✅ " + "; ".join(action_items))

        await callback_query.edit_message_text("\n".join(reply_parts))

    @owner_only
    async def handle_reminder_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle reminder buttons: done or snooze."""
        query = update.callback_query
        await query.answer()

        parts = query.data.split(":")  # "rem:done:123" or "rem:snooze:123"
        if len(parts) < 3:
            return

        action = parts[1]
        reminder_id = int(parts[2])
        db = self.pipeline.db

        if action == "done":
            await db.mark_reminder_sent(reminder_id)
            await query.edit_message_text(query.message.text + "\n\n✅ Отмечено как выполненное.")
        elif action == "snooze":
            from datetime import datetime, timedelta
            new_date = (datetime.now() + timedelta(days=1)).isoformat()
            await db.db.execute(
                "UPDATE reminders SET remind_at=?, sent=0 WHERE id=?", (new_date, reminder_id)
            )
            await db.db.commit()
            await query.edit_message_text(query.message.text + "\n\n⏰ Отложено на 1 день.")

    @owner_only
    async def handle_dedup_choice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle user's choice on semantic duplicate: n=keep_new / o=keep_old / b=keep_both."""
        query = update.callback_query
        await query.answer()

        data = query.data  # "dedup:n:abcd1234" etc.
        parts = data.split(":")
        if len(parts) < 3:
            await query.edit_message_text("❌ Неверные данные")
            return

        action = parts[1]
        dedup_key = parts[2]

        # Retrieve full IDs from bot_data
        ids = context.bot_data.pop(f"dd:{dedup_key}", None)
        if not ids:
            await query.edit_message_text("❌ Данные устарели. Обработайте файл заново.")
            return

        new_file_id = ids["new"]
        old_file_id = ids["old"]

        if action == "n":
            await self._cascade_delete(old_file_id)
            await query.edit_message_text("✅ Оставлен новый файл. Старый удалён.")
        elif action == "o":
            await self._cascade_delete(new_file_id)
            await query.edit_message_text("✅ Оставлен старый файл. Новый удалён.")
        elif action == "b":
            await query.edit_message_text("✅ Оба файла сохранены.")
        else:
            await query.edit_message_text("❌ Неизвестное действие")

    @owner_only
    async def handle_file_send(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send a file document to the user when they click a file button.

        Telegram allows ``query.answer()`` ONCE per callback. Earlier code
        called it eagerly at the top, which silently swallowed every
        subsequent ``query.answer(text=…, show_alert=True)`` — so when a
        sensitive file's PIN check failed, the user saw nothing happen
        instead of the "PIN не задан" prompt. We now call ``answer()``
        once with the right message at the end of every code path.
        """
        query = update.callback_query

        parts = query.data.split(":")  # "file:s:short_key"
        if len(parts) < 3:
            await query.answer()
            return

        short_key = parts[2]
        # Peek (not pop) so a transient failure / repeated tap still works.
        file_id = (
            self._pending_files.get(f"fs:{short_key}")
            or context.bot_data.get(f"fs:{short_key}")
        )
        if not file_id:
            await query.answer("❌ Данные устарели — повторите поиск", show_alert=True)
            return

        db = self.pipeline.db
        file = await db.get_file(file_id)
        if not file or not file.get("stored_path"):
            await query.answer("❌ Файл не найден", show_alert=True)
            return

        file_path = Path(file["stored_path"])
        if not file_path.exists():
            await query.answer("❌ Файл удалён с диска", show_alert=True)
            return

        # Sensitive (encrypted) → require PIN before sending.
        if file.get("sensitive"):
            db_obj = self.pipeline.db
            pin_hash = await db_obj.get_secret("PIN_HASH") if db_obj else None
            if not pin_hash:
                # Dismiss the spinner with a short alert AND post a normal
                # chat message — alerts are limited to 200 chars and one
                # call per callback, but the chat message persists so the
                # user has clear instructions.
                await query.answer(
                    "🔒 PIN не задан — нужно установить",
                    show_alert=True,
                )
                await context.bot.send_message(
                    chat_id=query.message.chat_id,
                    text=(
                        f"🔒 Файл *{file.get('original_name', 'документ')}* "
                        f"содержит личные данные и требует PIN для открытия.\n\n"
                        f"Открой <a href=\"https://fag.n8nskorx.top/settings\">"
                        f"Settings → Security</a> и задай 4–6-значный PIN, "
                        f"потом нажми кнопку ещё раз."
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                return
            # Stash the pending file id; next text message is interpreted as PIN.
            context.user_data["pending_open"] = {
                "file_id": file_id,
                "asked_at": time.time(),
                "attempts": 0,
            }
            await query.answer("🔒 Введи PIN сообщением")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=(
                    f"🔒 *{file.get('original_name', 'файл')}* — sensitive.\n"
                    "Введи PIN сообщением, чтобы открыть."
                ),
                parse_mode="Markdown",
            )
            return

        # Non-sensitive — send straight away.
        await query.answer()
        await self._send_file_to_user(context, query.message.chat_id, file)

    # Documents the bot pushes into a chat self-destruct after this many
    # seconds — Telegram chat history shouldn't keep copies of the user's
    # passport / SSN / pay-stubs sitting around.
    AUTO_DELETE_SECONDS = 15 * 60  # 15 minutes

    async def _send_file_to_user(
        self, context, chat_id: int, file: dict, *, decrypt: bool = False,
    ):
        """Send a file (optionally decrypting on-the-fly) and schedule
        auto-deletion of the chat message after AUTO_DELETE_SECONDS."""
        file_path = Path(file["stored_path"])
        filename = file.get("original_name", file_path.name)
        ttl_min = self.AUTO_DELETE_SECONDS // 60
        caption = (
            f"⏳ Это сообщение удалится через {ttl_min} мин — "
            "сохрани файл, если он нужен позже."
        )

        if decrypt:
            try:
                from app.main import get_state
                key = get_state("system_key")
            except Exception:
                key = None
            data = await self.pipeline.file_storage.read_bytes(file_path, decrypt_with=key)
            buf = BytesIO(data)
            buf.name = filename
            sent = await context.bot.send_document(
                chat_id=chat_id, document=buf, filename=filename, caption=caption,
            )
        else:
            sent = await context.bot.send_document(
                chat_id=chat_id,
                document=open(file_path, "rb"),
                filename=filename,
                caption=caption,
            )
        await self._schedule_auto_delete(chat_id, sent.message_id, file.get("id", ""))

    async def _schedule_auto_delete(self, chat_id: int, message_id: int, file_id: str = ""):
        """Persist a deletion task. The cleanup loop in main.lifespan runs
        every minute and removes due messages."""
        from datetime import datetime, timedelta, timezone
        delete_at = datetime.now(timezone.utc) + timedelta(seconds=self.AUTO_DELETE_SECONDS)
        try:
            await self.pipeline.db.schedule_message_deletion(
                chat_id=chat_id,
                message_id=message_id,
                delete_at_iso=delete_at.strftime("%Y-%m-%d %H:%M:%S"),
                note=file_id[:32],
            )
        except Exception as exc:
            logger.warning(f"auto-delete schedule failed: {exc}")

    async def _cascade_delete(self, file_id: str):
        """Delete file from disk + Qdrant + SQLite."""
        db = self.pipeline.db
        file = await db.get_file(file_id)

        # 1. Delete vectors
        try:
            await self.pipeline.vector_store.delete_document(file_id)
        except Exception as e:
            logger.warning(f"Failed to delete vectors for {file_id}: {e}")

        # 2. Delete file from disk
        if file and file.get("stored_path"):
            try:
                p = Path(file["stored_path"])
                if p.exists():
                    p.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete file from disk: {e}")

        # 3. Delete from DB
        await db.delete_file(file_id)
        logger.info(f"Cascade deleted file {file_id}")

    # ── Helpers ─────────────────────────────────────────────────────────

    async def _process_and_reply(self, ack_message, file_data: bytes, filename: str, context: ContextTypes.DEFAULT_TYPE):
        """Run pipeline and edit the ack message with results."""
        try:
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
            else:
                await ack_message.edit_text(result.summary_text())
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

        # Ingest substantive user messages into cognee personal memory.
        # Skip short queries (e.g. "ok", "/start", file lookups) — they are
        # noise. Assistant turns are derivative of files already in memory,
        # so they are not ingested.
        try:
            from app.ingestion import ingest_text_to_cognee
            from app.main import get_state
            await ingest_text_to_cognee(
                get_state("cognee"),
                content=query,
                source_type="chat",
                source_id=f"chat_{chat_id}",
                filename=f"chat_{chat_id}_msg.txt",
            )
        except Exception as e:
            logger.debug(f"cognee chat ingest skipped: {e}")

        if self.search_fn:
            try:
                # Show typing indicator while searching
                await update.effective_chat.send_action("typing")
                result = await self.search_fn(query, history=history, compact=True)
                text = result["text"] if isinstance(result, dict) else result
                file_ids = result.get("file_ids", {}) if isinstance(result, dict) else {}

                # Defensive: LLM occasionally returns empty/whitespace-only output
                # (refusal, hit max_tokens before producing text, model glitch).
                # Telegram rejects empty messages with "Message text is empty",
                # which we used to surface as "❌ Ошибка поиска". Replace with a
                # sane fallback so the user still sees the matched files.
                if not text or not text.strip():
                    logger.warning(
                        f"Empty LLM search response for query={query!r}; "
                        f"falling back to file list ({len(file_ids)} matches)"
                    )
                    if file_ids:
                        names = ", ".join(list(file_ids.values())[:5])
                        text = (
                            f"🤔 Не получил внятного ответа от LLM по запросу "
                            f"«{query}», но нашёл документы: {names}.\n"
                            "Открой их кнопками ниже или переформулируй вопрос."
                        )
                    else:
                        text = (
                            f"🤔 По запросу «{query}» ничего не нашлось. "
                            "Попробуй переформулировать."
                        )

                # Build inline keyboard with file buttons.
                # Pull `sensitive` + `metadata_json.display_label` per file
                # in one shot so buttons read like "🔒 Паспорт — Вячеслав"
                # instead of "🔒 photo_AQADyxdrG8oAARBKfg.jpg".
                file_meta: dict[str, dict] = {}
                if file_ids:
                    try:
                        placeholders = ",".join("?" * len(file_ids))
                        cur = await self.pipeline.db.db.execute(
                            "SELECT id, original_name, sensitive, metadata_json "
                            f"FROM files WHERE id IN ({placeholders})",
                            tuple(file_ids.keys()),
                        )
                        import json as _j
                        for row in await cur.fetchall():
                            try:
                                meta = _j.loads(row[3] or "{}")
                            except Exception:
                                meta = {}
                            file_meta[row[0]] = {
                                "original_name": row[1],
                                "sensitive": bool(row[2]),
                                "display_label": (meta.get("display_label") or "").strip(),
                                "document_type": meta.get("document_type", ""),
                                "owner": meta.get("owner", ""),
                            }
                    except Exception:
                        file_meta = {}

                keyboard = []
                for fid, fname in file_ids.items():
                    short_key = fid[:8]
                    self._pending_files[f"fs:{short_key}"] = fid
                    info = file_meta.get(fid, {})
                    # Prefer display_label → human "Type — owner" → filename.
                    label = info.get("display_label") or ""
                    if not label:
                        dt = info.get("document_type", "")
                        owner = info.get("owner", "")
                        if dt and owner:
                            label = f"{dt} — {owner.split()[0] if owner else ''}".strip(" —")
                        elif dt:
                            label = dt
                    if not label:
                        label = fname
                    label = label[:38] if len(label) <= 38 else label[:35] + "…"
                    icon = "🔒" if info.get("sensitive") else "📎"
                    keyboard.append(
                        InlineKeyboardButton(f"{icon} {label}", callback_data=f"file:s:{short_key}")
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
                        await update.message.reply_text(txt, reply_markup=markup, parse_mode="HTML")
                    except Exception:
                        # Fallback without formatting if HTML fails
                        await update.message.reply_text(txt, reply_markup=markup)

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
