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


async def _fire_task_reminder(context):
    """JobQueue callback — push the task to the user with snooze/done
    inline buttons. Looked up live by task_id so a /todos done in the
    meantime suppresses the firing."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    data = context.job.data or {}
    task_id = data.get("task_id")
    chat_id = data.get("chat_id")
    if not task_id or not chat_id:
        return
    db = context.application.bot_data.get("db")
    if db is None:
        return
    try:
        task = await db.get_task(int(task_id))
    except Exception:
        return
    if not task or task.get("status") != "open":
        return  # done/archived/snoozed elsewhere — skip
    buttons = [[
        InlineKeyboardButton("✅", callback_data=f"task:done:{task_id}"),
        InlineKeyboardButton("😴 1ч", callback_data=f"task:snooze:{task_id}:1h"),
        InlineKeyboardButton("🌅 Завтра", callback_data=f"task:snooze:{task_id}:tomorrow"),
        InlineKeyboardButton("📅 4ч",  callback_data=f"task:snooze:{task_id}:4h"),
    ]]
    text = f"⏰ <b>Напоминание</b>\n📌 {task['description']}"
    if task.get("due_text"):
        text += f"\n<i>{task['due_text']}</i>"
    try:
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        logger.exception("task reminder send failed")


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
        self._tg_app = None  # set on register() so callbacks can reach JobQueue

    COMMANDS = [
        BotCommand("dashboard", "Дашборд (mood / energy / sentiment)"),
        BotCommand("today", "Что было сегодня"),
        BotCommand("notes", "Заметки (по дням, поиск)"),
        BotCommand("files", "Документы"),
        BotCommand("search", "Семантический поиск"),
        BotCommand("recent", "Последние файлы"),
        BotCommand("scan", "Многостраничное сканирование"),
        BotCommand("done", "Завершить и проверить"),
        BotCommand("cancel", "Отменить сканирование"),
        BotCommand("stats", "Статистика базы"),
        BotCommand("analytics", "LLM-аналитика"),
        BotCommand("insights", "AI обзор"),
        BotCommand("patterns", "Паттерны поведения (аномалии, связи, todo)"),
        BotCommand("todos", "Открытые задачи из заметок"),
        BotCommand("remind", "Напоминание: /remind <время> <текст>"),
        BotCommand("skills", "Скиллы"),
        BotCommand("help", "Список команд"),
        BotCommand("start", "Начать работу"),
    ]

    def register(self, app: Application):
        """Register all handlers with the bot application."""
        self._tg_app = app
        app.bot_data["db"] = self.pipeline.db
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
        app.add_handler(CommandHandler("dashboard", self.cmd_dashboard))
        app.add_handler(CommandHandler("today", self.cmd_today))
        app.add_handler(CommandHandler("patterns", self.cmd_patterns))
        app.add_handler(CommandHandler("todos", self.cmd_todos))

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
        app.add_handler(CallbackQueryHandler(self.handle_note_open, pattern="^note:o:"))
        app.add_handler(CallbackQueryHandler(self.handle_note_edit_start, pattern="^note:e:"))
        app.add_handler(CallbackQueryHandler(self.handle_note_delete_confirm, pattern="^note:d:"))
        app.add_handler(CallbackQueryHandler(self.handle_note_delete, pattern="^note:dc:"))
        app.add_handler(CallbackQueryHandler(self.handle_note_delete_cancel, pattern="^note:dx:"))
        app.add_handler(CallbackQueryHandler(self.handle_notes_page, pattern="^np:"))
        app.add_handler(CallbackQueryHandler(self.handle_dashboard_period, pattern="^dash:"))
        app.add_handler(CallbackQueryHandler(self.handle_task_action, pattern="^task:"))
        app.add_handler(CommandHandler("remind", self.cmd_remind))
        app.add_handler(CommandHandler("cancel_reminder", self.cmd_cancel_reminder))

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

    NOTES_PAGE_SIZE = 10

    @owner_only
    async def cmd_notes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._render_notes_page(update, context, page=0)

    # ── Analytics dashboards ───────────────────────────────────────────

    @owner_only
    async def cmd_dashboard(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        """`/dashboard` — multi-panel PNG (mood/energy/sentiment/categories)."""
        from app.analytics.dashboard import build_dashboard_png
        # Default = 30 days. Period buttons let the user reshape.
        await update.effective_chat.send_action("upload_photo")
        try:
            png = await build_dashboard_png(self.pipeline.db, days=30)
        except Exception as e:
            logger.exception("dashboard build failed")
            await update.message.reply_text(f"⚠ не получилось: {e}")
            return
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("7 дней", callback_data="dash:7"),
            InlineKeyboardButton("30 дней", callback_data="dash:30"),
            InlineKeyboardButton("90 дней", callback_data="dash:90"),
            InlineKeyboardButton("Год", callback_data="dash:365"),
        ]])
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=png,
            caption="📊 Дашборд за 30 дней",
            reply_markup=markup,
        )

    @owner_only
    async def handle_dashboard_period(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        """Reshape dashboard for the chosen period (callback `dash:<days>`)."""
        from app.analytics.dashboard import build_dashboard_png
        query = update.callback_query
        await query.answer("Считаю…")
        try:
            days = int(query.data.split(":", 1)[1])
        except Exception:
            days = 30
        days = max(1, min(days, 730))
        try:
            png = await build_dashboard_png(self.pipeline.db, days=days)
        except Exception as e:
            logger.exception("dashboard rebuild failed")
            await query.message.reply_text(f"⚠ {e}")
            return
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("7 дней", callback_data="dash:7"),
            InlineKeyboardButton("30 дней", callback_data="dash:30"),
            InlineKeyboardButton("90 дней", callback_data="dash:90"),
            InlineKeyboardButton("Год", callback_data="dash:365"),
        ]])
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=png,
            caption=f"📊 Дашборд за {days} дн",
            reply_markup=markup,
        )

    @owner_only
    async def cmd_today(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        """`/today` — single-panel timeline of today's notes."""
        from app.analytics.dashboard import build_today_png
        await update.effective_chat.send_action("upload_photo")
        try:
            png = await build_today_png(self.pipeline.db)
        except Exception as e:
            logger.exception("today build failed")
            await update.message.reply_text(f"⚠ {e}")
            return
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=png,
            caption="☀️ Сегодняшний день",
        )

    @owner_only
    async def cmd_patterns(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        """`/patterns` — surface previously-invisible behaviour signals
        from `anomaly_alerts`, `personal_baselines`, `note_relations`,
        `note_tasks` (Sprint K)."""
        db = self.pipeline.db
        parts: list[str] = ["<b>📊 Паттерны и аномалии</b>\n"]

        # Anomalies — last 7 days
        cur = await db.db.execute(
            "SELECT alert_type, date, message FROM anomaly_alerts "
            "WHERE date >= date('now','-7 days') "
            "ORDER BY date DESC, id DESC LIMIT 8"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            parts.append("<b>⚠️ Аномалии (последние 7 дней)</b>")
            for r in rows:
                d = (r.get("date") or "")[:10]
                parts.append(f"<code>{d}</code> · {r.get('message','')[:120]}")
            parts.append("")

        # Baselines vs current
        cur = await db.db.execute(
            "SELECT metric_key, avg_value, std_value, data_points "
            "FROM personal_baselines ORDER BY metric_key"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            parts.append("<b>📐 Личные baseline (30-дневные)</b>")
            for r in rows:
                avg = r.get("avg_value")
                std = r.get("std_value") or 0
                parts.append(
                    f"• <i>{r['metric_key']}</i>: "
                    f"среднее <b>{avg:.1f}</b> ± {std:.1f} "
                    f"(<code>{r['data_points']}</code> точек)"
                )
            parts.append("")

        # Top connected notes (note_relations 1487 строк уже есть!)
        cur = await db.db.execute(
            "SELECT n.id, n.title, n.created_at, "
            "       (SELECT COUNT(*) FROM note_relations r "
            "        WHERE r.source_note_id=n.id OR r.target_note_id=n.id) AS deg "
            "FROM notes n WHERE n.content!='' "
            "ORDER BY deg DESC LIMIT 5"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        rows = [r for r in rows if (r.get("deg") or 0) > 0]
        if rows:
            parts.append("<b>🔗 Самые «связные» заметки</b>")
            for r in rows:
                title = (r.get("title") or "(без заголовка)")[:60]
                parts.append(
                    f"• {(r['created_at'] or '')[:10]} · {title} "
                    f"<i>({r['deg']} связей)</i>"
                )
            parts.append("")

        # Open tasks
        cur = await db.db.execute(
            "SELECT t.id, t.description, t.priority, n.created_at "
            "FROM note_tasks t JOIN notes n ON n.id = t.note_id "
            "WHERE t.status='open' "
            "ORDER BY t.created_at DESC LIMIT 8"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            parts.append("<b>✅ Открытые задачи (из заметок)</b>")
            for r in rows:
                parts.append(
                    f"• <code>{(r.get('created_at') or '')[:10]}</code> · "
                    f"{(r.get('description') or '')[:90]}"
                )
            parts.append("")

        # Lag correlations (might be empty until cron computes them)
        cur = await db.db.execute(
            "SELECT metric_a, metric_b, lag_days, correlation, p_value "
            "FROM lag_correlations WHERE p_value < 0.05 "
            "ORDER BY ABS(correlation) DESC LIMIT 5"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            parts.append("<b>🔬 Корреляции с лагом</b>")
            for r in rows:
                arrow = "→" if r["lag_days"] > 0 else "↔"
                parts.append(
                    f"• <i>{r['metric_a']}</i> {arrow} <i>{r['metric_b']}</i> "
                    f"(lag={r['lag_days']}д, r={r['correlation']:.2f})"
                )
            parts.append("")

        if len(parts) == 1:
            parts.append("Пока без паттернов — продолжай записывать "
                         "заметки и check-in'ы, через несколько дней "
                         "появятся.")

        text = "\n".join(parts)
        if len(text) > 3900:
            text = text[:3900] + "\n…"
        await update.message.reply_text(
            text, parse_mode="HTML", disable_web_page_preview=True,
        )

    @owner_only
    async def cmd_todos(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        """`/todos` — все открытые задачи извлечённые из заметок."""
        db = self.pipeline.db
        cur = await db.db.execute(
            "SELECT t.id, t.description, t.priority, t.due_date, "
            "       t.note_id, n.title, n.created_at "
            "FROM note_tasks t LEFT JOIN notes n ON n.id = t.note_id "
            "WHERE t.status='open' "
            "ORDER BY CASE t.priority WHEN 'high' THEN 0 "
            "  WHEN 'medium' THEN 1 ELSE 2 END, t.created_at DESC LIMIT 30"
        )
        rows = [dict(r) for r in await cur.fetchall()]
        if not rows:
            await update.message.reply_text(
                "✅ Открытых задач нет. Запиши заметку с конкретным делом "
                "(«надо позвонить врачу») — извлечётся автоматически."
            )
            return
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        prio_marker = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        header = f"<b>✅ Открытые задачи — {len(rows)}</b>"
        await update.message.reply_text(header, parse_mode="HTML")
        for r in rows[:15]:
            mark = prio_marker.get(r.get("priority", "medium"), "⚪")
            desc = (r.get("description") or "")[:120]
            note_date = (r.get("created_at") or "")[:10]
            tid = r["id"]
            row1 = [
                InlineKeyboardButton("✅", callback_data=f"task:done:{tid}"),
                InlineKeyboardButton("😴 1ч", callback_data=f"task:snooze:{tid}:1h"),
                InlineKeyboardButton("🌅 Завтра", callback_data=f"task:snooze:{tid}:tomorrow"),
                InlineKeyboardButton("✗", callback_data=f"task:drop:{tid}"),
            ]
            await update.message.reply_text(
                f"{mark} {desc}\n<i>из заметки {note_date}</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([row1]),
                disable_web_page_preview=True,
            )

    async def _render_notes_page(self, update_or_query, context, page: int = 0):
        """Render a page of notes — `<date> · <title>` per row, button per
        note that pulls the full transcript on tap. Pagination via prev/
        next buttons in the same callback family (`np:<page>`).
        """
        from datetime import datetime
        page = max(0, int(page))
        size = self.NOTES_PAGE_SIZE
        db = self.pipeline.db
        cur = await db.db.execute("SELECT COUNT(*) FROM notes WHERE content!=''")
        row = await cur.fetchone()
        total = row[0] if row else 0
        if total == 0:
            target = (
                update_or_query.message.reply_text
                if hasattr(update_or_query, "message")
                else update_or_query.message.edit_text
            )
            await target("📝 Заметок пока нет.")
            return
        max_page = max(0, (total - 1) // size)
        page = min(page, max_page)

        cur = await db.db.execute(
            "SELECT id, title, content, source, category, created_at "
            "FROM notes WHERE content!='' "
            "ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (size, page * size),
        )
        rows = [dict(r) for r in await cur.fetchall()]

        lines = [
            f"<b>Все заметки</b> — {total} шт. "
            f"(страница {page+1}/{max_page+1})\n",
        ]
        keyboard = []
        for n in rows:
            ts = (n.get("created_at") or "")
            date = ts[:10]
            time = ts[11:16]
            title = (n.get("title") or "").strip() or "(без заголовка)"
            short = f"n{n['id']}"
            self._pending_files[f"note:{short}"] = n["id"]
            lines.append(f"<code>{date}</code> {time} — {title[:90]}")
            keyboard.append([InlineKeyboardButton(
                f"{date} · {title[:50]}",
                callback_data=f"note:o:{short}",
            )])
        # Pagination row
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(
                "◀ Назад", callback_data=f"np:{page-1}"))
        if page < max_page:
            nav.append(InlineKeyboardButton(
                "Вперёд ▶", callback_data=f"np:{page+1}"))
        if nav:
            keyboard.append(nav)

        text = "\n".join(lines)
        markup = InlineKeyboardMarkup(keyboard)
        # Whether we're rendering fresh or paginating
        if hasattr(update_or_query, "message") and hasattr(update_or_query, "data"):
            # CallbackQuery from the np: button — edit in place
            try:
                await update_or_query.edit_message_text(
                    text, parse_mode="HTML", reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except Exception:
                await update_or_query.message.reply_text(
                    text, parse_mode="HTML", reply_markup=markup,
                    disable_web_page_preview=True,
                )
        else:
            await update_or_query.message.reply_text(
                text, parse_mode="HTML", reply_markup=markup,
                disable_web_page_preview=True,
            )

    @owner_only
    async def handle_notes_page(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pagination callback for /notes (np:<page>)."""
        query = update.callback_query
        await query.answer()
        try:
            page = int(query.data.split(":", 1)[1])
        except Exception:
            page = 0
        await self._render_notes_page(query, context, page=page)

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
        # Note edit flow takes priority over both PIN and date queries.
        pending_edit = context.user_data.get("pending_note_edit")
        if pending_edit:
            await self._handle_note_edit_text(update, context, query, pending_edit)
            return

        # the next text message as the PIN attempt.
        pending = context.user_data.get("pending_open")
        if pending:
            await self._handle_pin_attempt(update, context, query, pending)
            return

        # Date-scoped notes lookup BEFORE search ("заметки за сегодня",
        # "заметки 5 мая", "что я наговорил вчера" etc.).
        date_hit = self._parse_notes_date_query(query)
        if date_hit:
            await self._show_notes_for_day(update, context, *date_hit)
            return

        # Decide: explicit search → run search, otherwise ask via buttons.
        # Same logic as voice transcript flow — only obvious search-intent
        # phrases bypass the menu. Everything else is ambiguous and the
        # user picks. This matches user feedback: "I typed a note and the
        # bot ran a search without asking."
        if self._is_search_intent(query):
            await self._do_search(update, query, context)
            return

        # Ambiguous → 🔍 Поиск / 📝 Заметка
        import secrets
        text_key = secrets.token_hex(4)
        self._pending_files[f"vc:{text_key}"] = query
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 Поиск", callback_data=f"vc:s:{text_key}"),
            InlineKeyboardButton("📝 Заметка", callback_data=f"vc:n:{text_key}"),
        ]])
        preview = query if len(query) <= 200 else query[:200] + "…"
        await update.message.reply_text(
            f"📝 «{preview}»\n\nЭто поиск или заметка?",
            reply_markup=keyboard,
        )

    @staticmethod
    def _is_search_intent(text: str) -> bool:
        """True if the message clearly looks like a search query, not a
        free-form note. Same triggers as voice handler."""
        t = (text or "").strip().lower()
        if not t:
            return False
        triggers = (
            "найди", "найти", "поищи", "поиск", "ищи", "найдешь",
            "найдёшь", "покажи", "где мой", "где мои", "где моя",
            "find ", "search ", "show me", "where is", "where are",
        )
        return (
            t.startswith(triggers)
            or t.startswith("?") or t.endswith("?")
        )

    @staticmethod
    def _parse_notes_date_query(text: str) -> tuple[str, str] | None:
        """If the message is a "show me notes for <day>" query, return
        (date_iso, human_label). Otherwise return None.

        Recognised phrasings (Russian + English):
          - сегодня / today
          - вчера / yesterday
          - позавчера / day before yesterday
          - <number> <month_name> [<year>]
          - YYYY-MM-DD
        Plus a "notes-intent" keyword to avoid false positives on regular
        search queries that happen to mention a date.
        """
        from datetime import date, timedelta
        import re as _re

        t = text.strip().lower()
        intent = any(kw in t for kw in (
            "заметк", "заметки", "что я наговор", "что я записал",
            "что говорил", "transcript", "notes", "note from",
        ))
        if not intent:
            return None

        today = date.today()
        if any(w in t for w in ("сегодня", "today")):
            d = today
            label = "сегодня"
        elif any(w in t for w in ("вчера", "yesterday")):
            d = today - timedelta(days=1)
            label = "вчера"
        elif "позавчера" in t:
            d = today - timedelta(days=2)
            label = "позавчера"
        else:
            # YYYY-MM-DD literal
            m = _re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", t)
            if m:
                y, mo, dy = (int(x) for x in m.groups())
                try:
                    d = date(y, mo, dy)
                except ValueError:
                    return None
                label = d.isoformat()
            else:
                # "5 мая" / "5 may" / "May 5"
                months = {
                    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5,
                    "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10,
                    "ноя": 11, "дек": 12,
                    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5,
                    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
                    "nov": 11, "dec": 12,
                }
                mo = None
                for k, v in months.items():
                    if k in t:
                        mo = v
                        break
                if not mo:
                    return None
                m2 = _re.search(r"\b(\d{1,2})\b", t)
                if not m2:
                    return None
                dy = int(m2.group(1))
                # Default to current year; if that future date hasn't
                # happened yet, roll back a year.
                y = today.year
                try:
                    d = date(y, mo, dy)
                except ValueError:
                    return None
                if d > today:
                    d = date(y - 1, mo, dy)
                label = d.strftime("%d %B").lower()
        return (d.isoformat(), label)

    async def _show_notes_for_day(self, update, context, date_iso: str, label: str):
        """List notes for a specific calendar day. Brief summary per row;
        each gets a button so the user can pull the full transcript."""
        db = self.pipeline.db
        try:
            cur = await db.db.execute(
                "SELECT id, title, content, source, created_at "
                "FROM notes WHERE date(created_at) = ? "
                "ORDER BY created_at",
                (date_iso,),
            )
            rows = [dict(r) for r in await cur.fetchall()]
        except Exception as e:
            await update.message.reply_text(f"⚠️ Ошибка: {e}")
            return

        if not rows:
            await update.message.reply_text(
                f"🗓 Заметок за {label} ({date_iso}) нет."
            )
            return

        # Build menu: title or first line, time. No emoji per user request —
        # plain ASCII reads cleaner and avoids font-fallback issues.
        lines = [f"<b>Заметки за {label}</b> ({date_iso}) — {len(rows)}\n"]
        keyboard = []
        for n in rows:
            note_id = n["id"]
            title = (n.get("title") or "").strip()
            content = (n.get("content") or "").strip()
            preview = title or content.split("\n", 1)[0]
            preview = preview[:80].rstrip()
            time_part = (n.get("created_at") or "")[11:16]
            lines.append(f"<b>{time_part}</b> — {preview}")
            short = f"n{note_id}"
            self._pending_files[f"note:{short}"] = note_id
            keyboard.append(InlineKeyboardButton(
                f"{time_part} {preview[:38]}",
                callback_data=f"note:o:{short}",
            ))

        markup = InlineKeyboardMarkup([[b] for b in keyboard])
        text = "\n".join(lines)
        if len(text) > 3500:
            text = text[:3500] + "\n…"
        await update.message.reply_text(
            text, parse_mode="HTML", reply_markup=markup,
            disable_web_page_preview=True,
        )

    async def _handle_note_edit_text(self, update, context, new_text: str, pending: dict):
        """User had tapped ✏️ on a note and now sent new text. Replace
        notes.content + title, refresh FTS, re-embed Qdrant chunks for
        this note. Cancel via /cancel."""
        import re as _re
        import time
        import json as _j
        nid = int(pending.get("note_id") or 0)
        if not nid:
            context.user_data.pop("pending_note_edit", None)
            return
        # User can abort
        if new_text.strip().lower() in ("/cancel", "отмена", "cancel"):
            context.user_data.pop("pending_note_edit", None)
            await update.message.reply_text("↩ Редактирование отменено.")
            return
        # Stale prompt (older than 5 min) — drop it
        if time.time() - pending.get("asked_at", 0) > 300:
            context.user_data.pop("pending_note_edit", None)
            await update.message.reply_text(
                "⏱ Запрос устарел. Открой заметку ещё раз и нажми ✏️."
            )
            return

        body = new_text.strip()
        first = _re.sub(r"^#+\s+", "", body.split("\n")[0]).strip()
        first = _re.split(r"\s{2,}|https?://", first)[0].strip()
        title = first[:60] or "(без заголовка)"

        db = self.pipeline.db
        try:
            await db.db.execute(
                "UPDATE notes SET content=?, title=? WHERE id=?",
                (body, title, nid),
            )
            await db.db.commit()
            # FTS rebuild for this row only is cheap via the trigger;
            # the existing notes_au trigger covers it on UPDATE.
        except Exception as exc:
            context.user_data.pop("pending_note_edit", None)
            await update.message.reply_text(f"⚠ Не удалось сохранить: {exc}")
            return

        # Re-embed: drop the note's Qdrant points and re-upsert chunks.
        try:
            from qdrant_client.models import (Filter, FieldCondition,
                                              MatchValue, PointStruct)
            import uuid
            vs = self.pipeline.vector_store
            coll = vs.qdrant_config.collection_name
            vs._client.delete(
                collection_name=coll,
                points_selector=Filter(must=[FieldCondition(
                    key="note_id", match=MatchValue(value=nid),
                )]),
                wait=True,
            )
            text_with_title = f"{title}\n\n{body}" if title else body
            words = text_with_title.split()
            CHUNK = 300
            OVERLAP = 50
            chunks = []
            i = 0
            while i < len(words):
                chunks.append(" ".join(words[i:i + CHUNK]))
                i += CHUNK - OVERLAP
            if chunks:
                emb = vs._get_gemini_embedder()
                vectors = emb.embed_texts(chunks, task_type="RETRIEVAL_DOCUMENT")
                points = []
                for ci, (chunk_text, vec) in enumerate(zip(chunks, vectors)):
                    pid = str(uuid.uuid5(uuid.NAMESPACE_DNS,
                                         f"note:{nid}:chunk:{ci}"))
                    points.append(PointStruct(
                        id=pid, vector=vec,
                        payload={
                            "type": "note", "note_id": nid,
                            "chunk_index": ci, "text": chunk_text[:5000],
                            "title": title[:200],
                        },
                    ))
                vs._client.upsert(collection_name=coll, points=points, wait=True)
        except Exception as exc:
            logger.warning(f"note edit re-embed failed for {nid}: {exc}")

        # Wipe search cache so the next query sees the new text
        try:
            await db.db.execute("DELETE FROM search_cache")
            await db.db.commit()
        except Exception:
            pass

        # Outbox: signal cognee + wiki to refresh — Qdrant we already
        # rebuilt above synchronously, so a 'qdrant' row is purely an
        # audit record for memory-doctor.
        try:
            await db.enqueue_outbox(
                event_type="note_updated",
                source_kind="note",
                source_id=str(nid),
                payload={"title": title},
            )
        except Exception as exc:
            logger.debug(f"outbox enqueue (note_updated) failed: {exc}")

        context.user_data.pop("pending_note_edit", None)
        await update.message.reply_text(
            f"✅ Заметка обновлена.\n📝 <b>{title}</b>",
            parse_mode="HTML",
        )

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

        # 5a. Sprint D — enqueue outbox events so the sweeper re-embeds
        # the note in Qdrant, ingests into cognee, and rebuilds the
        # wiki page asynchronously. Failure here MUST NOT lose the
        # note — content is already in SQLite + .md.
        try:
            await db.enqueue_outbox(
                event_type="note_added",
                source_kind="note",
                source_id=str(note_id),
                payload={"title": title, "source": "voice"},
            )
        except Exception as exc:
            logger.debug(f"outbox enqueue (note_added) failed: {exc}")

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

        # 5c. Sprint P — extract structured tasks from raw text and
        # auto-add (status='open'). Footer-undo lets the user drop any
        # mistake in one tap.
        extracted_tasks: list[tuple[int, str]] = []  # [(task_id, short_desc)]
        try:
            from app.llm.task_extractor import extract_tasks
            from app.services.date_nlp import parse_due, to_iso
            tasks = await extract_tasks(text, language="ru")
            for t in tasks:
                remind_at_iso: str | None = None
                if t.due_text:
                    dt = parse_due(t.due_text, base=now)
                    if dt is not None:
                        remind_at_iso = to_iso(dt)
                tid = await db.enqueue_task(
                    description=t.description,
                    note_id=note_id,
                    priority=t.priority,
                    due_text=t.due_text,
                    remind_at=remind_at_iso,
                    extraction_confidence=t.confidence,
                    source_span=json_mod.dumps(list(t.source_span))
                        if t.source_span else "",
                    rationale=t.rationale,
                    linked_file_id=linked_file_id,
                    status="open",
                )
                extracted_tasks.append((tid, t.description[:60]))
                # Schedule the per-task push if there's a future remind_at.
                if remind_at_iso and self._tg_app is not None:
                    try:
                        from datetime import datetime as _dt
                        when = _dt.strptime(remind_at_iso, "%Y-%m-%d %H:%M:%S")
                        if when > _dt.now():
                            self._tg_app.job_queue.run_once(
                                _fire_task_reminder, when=when,
                                data={"task_id": tid, "chat_id": chat_id},
                                name=f"task_{tid}",
                            )
                    except Exception as exc:
                        logger.debug(f"task scheduler failed: {exc}")
        except Exception as exc:
            logger.debug(f"task extraction skipped: {exc}")

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

        if extracted_tasks:
            reply_parts.append("")
            for _, desc in extracted_tasks:
                reply_parts.append(f"📌 {desc}")

        keyboard = None
        if extracted_tasks:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            buttons = []
            for tid, desc in extracted_tasks[:3]:
                short = (desc[:18] + "…") if len(desc) > 18 else desc
                buttons.append([InlineKeyboardButton(
                    f"✗ убрать «{short}»",
                    callback_data=f"task:drop:{tid}",
                )])
            keyboard = InlineKeyboardMarkup(buttons)

        await callback_query.edit_message_text(
            "\n".join(reply_parts), reply_markup=keyboard,
        )

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
    async def handle_task_action(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Sprint P — task button callbacks: done / snooze / drop / keep."""
        from datetime import datetime, timedelta
        from app.services.date_nlp import to_iso

        query = update.callback_query
        await query.answer()
        parts = query.data.split(":")  # task:done:42 / task:snooze:42:1h / task:drop:42
        if len(parts) < 3:
            return
        action = parts[1]
        try:
            task_id = int(parts[2])
        except ValueError:
            return
        db = self.pipeline.db
        task = await db.get_task(task_id)
        if not task:
            await query.edit_message_text(query.message.text + "\n\n(задача уже удалена)")
            return

        original = query.message.text or ""

        if action == "done":
            await db.mark_task_done(task_id)
            self._cancel_task_job(task_id)
            await query.edit_message_text(
                f"~~{original}~~\n\n✅ Выполнено", parse_mode="Markdown",
            )
            return

        if action == "drop":
            await db.archive_task(task_id)
            self._cancel_task_job(task_id)
            # Drop the matching pin line from the parent message footer.
            new_text = "\n".join(
                ln for ln in original.splitlines()
                if not ln.startswith(f"📌 {task['description'][:60]}")
            ) or "📝 (заметка сохранена)"
            try:
                await query.edit_message_text(new_text)
            except Exception:
                pass
            return

        if action == "snooze" and len(parts) >= 4:
            bucket = parts[3]
            now = datetime.now()
            target = {
                "15m": now + timedelta(minutes=15),
                "1h":  now + timedelta(hours=1),
                "4h":  now + timedelta(hours=4),
                "tonight":  now.replace(hour=21, minute=0, second=0, microsecond=0)
                            + (timedelta(days=1) if now.hour >= 21 else timedelta()),
                "tomorrow": (now + timedelta(days=1)).replace(
                    hour=9, minute=0, second=0, microsecond=0),
            }.get(bucket)
            if target is None:
                return
            iso = to_iso(target)
            await db.snooze_task(task_id, iso)
            self._cancel_task_job(task_id)
            if self._tg_app is not None:
                self._tg_app.job_queue.run_once(
                    _fire_task_reminder, when=target,
                    data={"task_id": task_id,
                          "chat_id": query.message.chat_id},
                    name=f"task_{task_id}",
                )
            await query.edit_message_text(
                f"{original}\n\n😴 Отложено до {iso[:16]}"
            )
            return

    def _cancel_task_job(self, task_id: int):
        if self._tg_app is None:
            return
        for job in self._tg_app.job_queue.get_jobs_by_name(f"task_{task_id}"):
            job.schedule_removal()

    @owner_only
    async def cmd_remind(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """`/remind <время> <текст>` — explicit reminder, no LLM.

        Reply-to-message form: `/remind <время>` uses the replied text
        as the body and links to the original note/file."""
        from datetime import datetime
        from app.services.date_nlp import parse_due, to_iso

        msg = update.message
        args_text = (msg.text or "").split(maxsplit=1)
        if len(args_text) < 2:
            await msg.reply_text(
                "⏰ <b>/remind</b> <i>время</i> <i>текст</i>\n"
                "пример: /remind через 2 часа купить молоко\n"
                "или: /remind завтра в 9 — на reply'е к заметке",
                parse_mode="HTML",
            )
            return
        rest = args_text[1].strip()

        replied = msg.reply_to_message
        replied_text = (replied.text or replied.caption or "") if replied else ""

        # Greedy: try the longest leading prefix that parses as a date,
        # leaving the suffix as the description.
        when = None
        time_text = ""
        body_text = ""
        words = rest.split()
        for cut in range(min(len(words), 6), 0, -1):
            head = " ".join(words[:cut])
            cand = parse_due(head, base=datetime.now())
            if cand is not None:
                when = cand
                time_text = head
                body_text = " ".join(words[cut:]).strip()
                break
        if when is None and replied_text:
            cand = parse_due(rest, base=datetime.now())
            if cand is not None:
                when = cand
                time_text = rest
                body_text = replied_text[:200]
        if when is None:
            await msg.reply_text(
                "❓ Не понял время. Попробуй: «через 2 часа», «завтра в 9», «в пятницу»."
            )
            return
        if not body_text:
            body_text = replied_text[:200] or "напоминание"

        if when <= datetime.now():
            await msg.reply_text("❓ Время уже прошло.")
            return

        db = self.pipeline.db
        tid = await db.enqueue_task(
            description=body_text[:200],
            due_text=time_text,
            remind_at=to_iso(when),
            extraction_confidence="explicit",
            priority="medium",
            rationale="/remind",
            status="open",
        )
        if self._tg_app is not None:
            self._tg_app.job_queue.run_once(
                _fire_task_reminder, when=when,
                data={"task_id": tid, "chat_id": msg.chat_id},
                name=f"task_{tid}",
            )
        await msg.reply_text(
            f"⏰ Напомню {to_iso(when)[:16]}\n📌 {body_text[:60]}\n"
            f"<i>id={tid} · /cancel_reminder {tid}</i>",
            parse_mode="HTML",
        )

    @owner_only
    async def cmd_cancel_reminder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.message
        args = (msg.text or "").split()
        if len(args) < 2 or not args[1].isdigit():
            await msg.reply_text("использование: /cancel_reminder <id>")
            return
        tid = int(args[1])
        db = self.pipeline.db
        task = await db.get_task(tid)
        if not task:
            await msg.reply_text(f"задача {tid} не найдена")
            return
        await db.archive_task(tid)
        self._cancel_task_job(tid)
        await msg.reply_text(f"✅ Напоминание {tid} отменено")

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
    async def handle_note_open(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show the full transcript for a single note when its button is
        tapped from a "заметки за <day>" menu."""
        query = update.callback_query
        parts = query.data.split(":")  # "note:o:<short>"
        if len(parts) < 3:
            await query.answer()
            return
        short = parts[2]
        note_id = self._pending_files.get(f"note:{short}")
        if not note_id:
            await query.answer("⏱ Запрос устарел, повтори поиск", show_alert=True)
            return
        try:
            cur = await self.pipeline.db.db.execute(
                "SELECT title, content, source, created_at FROM notes WHERE id=?",
                (note_id,),
            )
            row = await cur.fetchone()
        except Exception as e:
            await query.answer(f"⚠ {e}", show_alert=True)
            return
        if not row:
            await query.answer("Заметка не найдена", show_alert=True)
            return
        n = dict(row)
        title = (n.get("title") or "").strip() or "Без заголовка"
        body = (n.get("content") or "").strip() or "(пусто)"
        when = (n.get("created_at") or "")[:16]
        src = n.get("source", "")
        text = f"📝 <b>{title}</b>\n<i>{when} · {src}</i>\n\n{body}"
        if len(text) > 3900:
            text = text[:3900] + "\n…"
        # Edit / Delete actions for the note. The same `short` key is
        # already in `_pending_files` from the open step so we reuse it.
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ Редактировать",
                                 callback_data=f"note:e:{short}"),
            InlineKeyboardButton("🗑 Удалить",
                                 callback_data=f"note:d:{short}"),
        ]])
        await query.answer()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=markup,
        )

    @owner_only
    async def handle_note_edit_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        """User tapped ✏️ — stash the note id, wait for the next text
        message in `handle_text` which will accept it as the new content."""
        import time
        query = update.callback_query
        parts = query.data.split(":")  # "note:e:<short>"
        if len(parts) < 3:
            await query.answer()
            return
        short = parts[2]
        note_id = self._pending_files.get(f"note:{short}")
        if not note_id:
            await query.answer("⏱ Запрос устарел", show_alert=True)
            return
        context.user_data["pending_note_edit"] = {
            "note_id": int(note_id),
            "asked_at": time.time(),
        }
        await query.answer("✏️ Жду новый текст")
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=(
                "✏️ Пришли новый текст заметки одним сообщением.\n"
                "Старое содержимое будет полностью заменено.\n"
                "/cancel — отмена."
            ),
        )

    @owner_only
    async def handle_note_delete_confirm(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        """User tapped 🗑 Удалить — show a confirm prompt."""
        query = update.callback_query
        parts = query.data.split(":")
        if len(parts) < 3:
            await query.answer()
            return
        short = parts[2]
        note_id = self._pending_files.get(f"note:{short}")
        if not note_id:
            await query.answer("⏱ Запрос устарел", show_alert=True)
            return
        # Re-fetch the title for the prompt
        try:
            cur = await self.pipeline.db.db.execute(
                "SELECT title FROM notes WHERE id=?", (int(note_id),),
            )
            row = await cur.fetchone()
            title = (dict(row).get("title") or "")[:60] if row else f"#{note_id}"
        except Exception:
            title = f"#{note_id}"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Подтвердить удаление",
                                 callback_data=f"note:dc:{short}"),
            InlineKeyboardButton("↩ Отмена", callback_data="note:dx:0"),
        ]])
        await query.answer()
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=f"🗑 Удалить заметку «{title}»?",
            reply_markup=markup,
        )

    @owner_only
    async def handle_note_delete(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        """User confirmed 🗑 — cascade delete from SQLite + Qdrant +
        wiki + the on-disk .md."""
        query = update.callback_query
        parts = query.data.split(":")  # "note:dc:<short>"
        if len(parts) < 3:
            await query.answer()
            return
        short = parts[2]
        note_id = self._pending_files.get(f"note:{short}")
        if not note_id:
            await query.answer("⏱ Запрос устарел", show_alert=True)
            return
        note_id = int(note_id)
        db = self.pipeline.db
        # 1. fetch metadata for cleanup paths
        try:
            cur = await db.db.execute(
                "SELECT title, content, md_path FROM notes WHERE id=?",
                (note_id,),
            )
            row = await cur.fetchone()
        except Exception:
            row = None
        title = "?"
        md_path = ""
        if row:
            n = dict(row)
            title = (n.get("title") or "")[:50] or "?"
            md_path = n.get("md_path") or ""

        errors: list[str] = []

        # 2. Qdrant — delete every point with payload.note_id == this id
        try:
            from qdrant_client.models import (Filter, FieldCondition,
                                              MatchValue)
            vs = self.pipeline.vector_store
            vs._client.delete(
                collection_name=vs.qdrant_config.collection_name,
                points_selector=Filter(must=[FieldCondition(
                    key="note_id", match=MatchValue(value=note_id),
                )]),
                wait=True,
            )
        except Exception as exc:
            errors.append(f"qdrant: {exc}")

        # 3. SQLite — note_enrichments cascades via FK; just drop notes row.
        try:
            await db.db.execute("DELETE FROM notes WHERE id=?", (note_id,))
            await db.db.commit()
        except Exception as exc:
            errors.append(f"sqlite: {exc}")

        # Outbox: queue cognee delete (we don't have a clean wrapper
        # for it yet — sweeper will skip-with-reason until one lands)
        # and a wiki-refresh signal so the entity backlinks lose this
        # mention on the next regen.
        try:
            await db.enqueue_outbox(
                event_type="note_deleted",
                source_kind="note",
                source_id=str(note_id),
                payload={"title": title},
                targets=["cognee", "wiki"],
            )
        except Exception:
            pass

        # 4. On-disk .md (Obsidian-style transcript copy)
        if md_path:
            try:
                p = Path(md_path)
                if p.exists():
                    p.unlink()
            except Exception as exc:
                errors.append(f"md: {exc}")

        # 5. Wiki vault — find any wiki/notes/*-<id>.md and remove it.
        # build_wiki regenerates the rest from SQLite, so deleting just
        # this page is enough.
        try:
            from app.config import get_settings
            wiki_notes = get_settings().wiki.resolved_path / "notes"
            if wiki_notes.exists():
                for p in wiki_notes.glob(f"*-{note_id}.md"):
                    p.unlink()
        except Exception as exc:
            errors.append(f"wiki: {exc}")

        # 6. cognee — best-effort; sidecar exposes delete via dataset
        # API that we don't have a clean wrapper for yet, so skip and
        # leave a TODO for the outbox-driven version (Sprint D).

        if errors:
            await query.answer(
                "⚠ удалено с замечаниями", show_alert=True,
            )
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🗑 «{title}» удалена частично: {'; '.join(errors)}",
            )
        else:
            await query.answer("🗑 удалена")
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🗑 «{title}» удалена.",
            )
        # Clear the pending key (best-effort)
        self._pending_files.pop(f"note:{short}", None)

    @owner_only
    async def handle_note_delete_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE,
    ):
        """User tapped ↩ Отмена on a delete prompt."""
        query = update.callback_query
        await query.answer("Отменено")
        try:
            await query.edit_message_text("↩ Удаление отменено.")
        except Exception:
            pass

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

    @staticmethod
    def _classify_search_intent(query: str) -> str:
        """Decide which store the user wants to hit.

        Returns one of:
          * 'notes' — query mentions заметки / notes / transcripts /
                     check-in / things only stored as voice or text notes
          * 'files' — query names a document type or document keywords
          * 'both'  — ambiguous, search across both stores

        Used by `_do_search` so a "найди заметку про CRM" query doesn't
        return passport scans, and "найди паспорт" doesn't get drowned
        in matching notes.
        """
        q = (query or "").strip().lower()
        notes_kw = (
            "заметк", "заметку", "заметки", "наговорил", "наговор",
            "записал", "запись на диктофон", "запис",
            "checkin", "check-in", "чек-ин", "чек ин",
            "transcript", "voice note", "запис голос",
            "что я сказал", "что я говорил",
        )
        files_kw = (
            "документ", "файл", "паспорт", "passport",
            "ssn", "социального страхования", "social security",
            "права", "license", "permit", "i-94", "i94",
            "i-765", "i-131", "ead", "виза", "visa",
            "свидетельств", "контракт", "договор", "contract",
            "pay stub", "paystub", "pay-stub",
            "расчётны", "расчетны", "зарпл",
            "w-9", "w9", "w-2", "w2", "1099",
            "налог", "tax form", "invoice", "счёт", "счет",
            "медицинский анализ", "lab result", "мрт", "mri",
            "выписк", "after visit",
        )
        n = any(k in q for k in notes_kw)
        f = any(k in q for k in files_kw)
        if n and not f:
            return "notes"
        if f and not n:
            return "files"
        return "both"

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
                # Decide which kind of source the user is asking about.
                # "найди заметку" → notes only. "найди паспорт / документ /
                # SSN / pay-stub" → files only. Anything else → both.
                intent = self._classify_search_intent(query)
                result = await self.search_fn(query, history=history, compact=True)
                text = result["text"] if isinstance(result, dict) else result
                file_ids = result.get("file_ids", {}) if isinstance(result, dict) else {}
                note_ids_raw = result.get("note_ids", {}) if isinstance(result, dict) else {}
                if intent == "notes":
                    file_ids = {}
                elif intent == "files":
                    note_ids_raw = {}

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

                # Note buttons (semantic search now matches transcripts too).
                # Use the intent-filtered set, not the raw result
                note_ids = note_ids_raw if isinstance(note_ids_raw, dict) else {}
                for nid, ntitle in note_ids.items():
                    short = f"n{nid}"
                    self._pending_files[f"note:{short}"] = int(nid) if str(nid).isdigit() else nid
                    title = (ntitle or "").strip()[:34] or "Заметка"
                    keyboard.append(
                        InlineKeyboardButton(
                            f"📝 {title}", callback_data=f"note:o:{short}",
                        )
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
