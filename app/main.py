"""Smart Storage — Entry Point with full lifespan management."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request

from app.config import get_settings

load_dotenv()

logger = logging.getLogger(__name__)

_state: dict = {}


def get_state(key: str):
    return _state.get(key)


async def _load_secrets_to_env(db, session_secret: str):
    """Load encrypted secrets from DB and export to os.environ (if not already set)."""
    import os
    from app.utils.crypto import decrypt
    SECRET_MAP = {
        "ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY": "OPENAI_API_KEY",
        "GOOGLE_API_KEY": "GOOGLE_API_KEY",
        "QDRANT_API_KEY": "QDRANT__API_KEY",
        "TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    }
    for secret_name, env_name in SECRET_MAP.items():
        if os.environ.get(env_name):
            continue  # .env already set — don't override
        encrypted = await db.get_secret(secret_name)
        if encrypted:
            value = decrypt(encrypted, session_secret)
            if value:
                os.environ[env_name] = value
                logger.info(f"Loaded secret '{secret_name}' from DB")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.setup_env_keys()

    logging.basicConfig(level=settings.logging.level, format=settings.logging.format)
    logger.info("Starting Smart Storage v0.2.0")

    # ── Encryption setup ────────────────────────────────────────────────
    import os as _os
    encryption_key_bytes = None

    from app.utils.crypto import (
        is_encryption_configured,
        setup_master_password,
        unlock_with_password,
        generate_recovery_key,
        parse_encryption_key,
    )

    # Priority 1: Master password (interactive or via env MASTER_PASSWORD)
    # Priority 2: ENCRYPTION_KEY env var (legacy / automation / Docker)
    encryption_key_hex = settings.encryption_key or _os.environ.get("ENCRYPTION_KEY", "")
    master_password = _os.environ.get("MASTER_PASSWORD", "")

    # Optional key file for 2FA (e.g. on USB drive)
    key_file_path = _os.environ.get("KEY_FILE", "")
    key_file_data = None
    if key_file_path:
        kf = Path(key_file_path)
        if kf.exists():
            key_file_data = kf.read_bytes()
            logger.info(f"Key file loaded: {key_file_path}")
        else:
            raise SystemExit(
                f"Файл-ключ не найден: {key_file_path}\n"
                "Подключите USB-накопитель с файлом-ключом."
            )

    if master_password and is_encryption_configured():
        # Password provided + already configured → unlock
        keyfile = str(Path("data/encryption.key"))
        try:
            encryption_key_bytes = unlock_with_password(
                master_password, keyfile, key_file_data=key_file_data,
            )
        except ValueError as e:
            raise SystemExit(f"❌ {e}") from None
        logger.info("Encryption unlocked (key in memory only)")

    elif master_password and not is_encryption_configured():
        # Password provided + first time → setup
        keyfile = str(Path("data/encryption.key"))
        encryption_key_bytes = setup_master_password(
            master_password, keyfile, key_file_data=key_file_data,
        )
        if key_file_data:
            logger.info("2FA: master password + key file")
        logger.info("Encryption key derived — in memory only, not on disk")

        recovery_file = Path("data/RECOVERY_KEY.txt")
        recovery = generate_recovery_key(encryption_key_bytes)
        recovery_file.parent.mkdir(parents=True, exist_ok=True)
        recovery_file.write_text(
            "КЛЮЧ ВОССТАНОВЛЕНИЯ (RECOVERY KEY)\n"
            "====================================\n"
            f"{recovery}\n\n"
            "Сохраните этот ключ в менеджере паролей и УДАЛИТЕ этот файл.\n"
            "Без этого ключа расшифровать данные НЕВОЗМОЖНО.\n"
        )
        recovery_file.chmod(0o600)  # Owner-read only
        logger.warning(
            f"Recovery key → {recovery_file} — "
            "сохраните его в надёжном месте и удалите файл!"
        )

    elif is_encryption_configured() and not master_password:
        # Try to load key from previous web unlock session
        from app.web.routes import load_session_key
        session_key = load_session_key()
        if session_key:
            encryption_key_bytes = session_key
            logger.info("🔓 Encryption unlocked from previous session")
        else:
            logger.warning(
                "🔒 Encryption configured but no MASTER_PASSWORD — "
                "unlock via Settings or restart with MASTER_PASSWORD=..."
            )

    elif encryption_key_hex:
        # Legacy mode — raw key from env (for Docker / automation)
        encryption_key_bytes = parse_encryption_key(encryption_key_hex)
        logger.info("Encryption enabled via ENCRYPTION_KEY (legacy mode)")
        logger.warning(
            "Рекомендуется перейти на мастер-пароль: "
            "уберите ENCRYPTION_KEY и перезапустите"
        )
    else:
        if settings.encryption.files or settings.encryption.database:
            logger.warning(
                "Encryption flags set but no password/key provided — "
                "encryption disabled"
            )

    # Warn if recovery key file still on disk
    if encryption_key_bytes:
        recovery_file = Path("data/RECOVERY_KEY.txt")
        if recovery_file.exists():
            logger.warning(
                f"⚠️  Recovery key file still at {recovery_file} — "
                "сохраните и удалите!"
            )

    # Store key in state for web UI unlock access
    _state["_encryption_key"] = encryption_key_bytes

    enc_files = encryption_key_bytes if settings.encryption.files else None
    enc_db = encryption_key_bytes if settings.encryption.database else None

    from app.storage.db import Database
    db = Database(settings.database.path, encryption_key=enc_db)
    await db.connect()
    _state["db"] = db
    logger.info(f"Database ready: {settings.database.path}")

    # Load encrypted secrets from DB → environment (supplement .env)
    if settings.web.session_secret:
        await _load_secrets_to_env(db, settings.web.session_secret)
    else:
        logger.warning("WEB__SESSION_SECRET not set — encrypted secrets will NOT be loaded from DB")

    from app.storage.files import FileStorage
    from app.storage.backends.local import LocalBackend
    backends: dict = {"local": LocalBackend(settings.storage.base_path, enc_files)}

    if settings.storage.backend == "s3" or settings.storage.s3.bucket:
        try:
            from app.storage.backends.s3 import S3Backend
            s3 = settings.storage.s3
            backends["s3"] = S3Backend(
                bucket=s3.bucket, prefix=s3.prefix, region=s3.region,
                access_key_id=s3.access_key_id,
                secret_access_key=s3.secret_access_key,
                endpoint_url=s3.endpoint_url, encryption_key=enc_files,
            )
        except Exception as e:
            logger.warning(f"S3 backend init failed: {e}")

    if settings.storage.backend == "gdrive" or settings.storage.gdrive.folder_id:
        try:
            from app.storage.backends.gdrive import GDriveBackend
            gd = settings.storage.gdrive
            backends["gdrive"] = GDriveBackend(
                credentials_json=gd.credentials_json,
                folder_id=gd.folder_id, encryption_key=enc_files,
            )
        except Exception as e:
            logger.warning(f"Google Drive backend init failed: {e}")

    active = settings.storage.backend if settings.storage.backend in backends else "local"
    file_storage = FileStorage(
        active_backend=active, backends=backends,
        allowed_extensions=settings.storage.allowed_extensions,
    )
    _state["file_storage"] = file_storage
    logger.info(f"Storage backend: {active}")

    from app.storage.vectors import VectorStore
    vector_store = VectorStore(
        settings.qdrant, settings.embedding,
        google_api_key=settings.google_api_key,
        strip_text=settings.encryption.qdrant_strip,
    )
    try:
        await vector_store.connect()
        logger.info(f"Qdrant connected: {settings.qdrant.url}")
    except Exception as e:
        logger.warning(f"Qdrant not available: {e}")
    _state["vector_store"] = vector_store

    from app.skills.engine import SkillEngine
    skill_engine = SkillEngine(settings.skills.directory)
    await skill_engine.load_all()
    _state["skill_engine"] = skill_engine

    from app.llm.router import LLMRouter
    llm_router = LLMRouter(settings.llm, db=db)
    _state["llm_router"] = llm_router

    from app.llm.classifier import Classifier
    classifier = Classifier(llm_router, skill_engine)
    _state["classifier"] = classifier

    from app.parser.factory import ParserFactory
    vision_model = settings.llm.models.get("extraction", None)
    parser_factory = ParserFactory(vision_model=vision_model.model if vision_model else None)
    _state["parser_factory"] = parser_factory

    from app.llm.summarizer import FileSummarizer
    summarizer = FileSummarizer(llm_router, prompt_template=settings.llm.file_summary_prompt)

    from app.pipeline import Pipeline
    pipeline = Pipeline(
        settings=settings, db=db, file_storage=file_storage,
        vector_store=vector_store, parser_factory=parser_factory,
        llm_router=llm_router, classifier=classifier, skill_engine=skill_engine,
    )
    pipeline.summarizer = summarizer
    _state["pipeline"] = pipeline

    from app.llm.search import LLMSearch
    llm_search = LLMSearch(vector_store, llm_router, db=db)
    _state["llm_search"] = llm_search

    from app.llm.analytics import LLMAnalytics
    llm_analytics = LLMAnalytics(vector_store, llm_router, db=db)
    _state["llm_analytics"] = llm_analytics

    from app.services.lifecycle import FileLifecycleService
    lifecycle = FileLifecycleService(
        db=db,
        file_storage=file_storage,
        vector_store=vector_store,
        llm_search=llm_search,
        classifier=classifier,
        summarizer=summarizer,
    )
    _state["lifecycle"] = lifecycle

    from app.llm.insights import InsightsEngine
    insights_engine = InsightsEngine(llm_router, db)
    _state["insights_engine"] = insights_engine

    from app.notes.morning import MorningBriefingEngine
    morning_engine = MorningBriefingEngine(db, llm_router)
    _state["morning_engine"] = morning_engine

    # Initialize Smart Notes v1.5 services
    note_agent = None  # legacy compat
    note_capture = None
    note_processor = None
    if settings.notes.enabled:
        from app.notes.capture import NoteCaptureService
        from app.notes.enrichment import NoteEnrichmentService
        from app.notes.relations import NoteRelationService
        from app.notes.projection import NoteProjectionService
        from app.notes.processor import NoteProcessor
        from app.notes.vault import ObsidianVault

        vault_path = settings.notes.vault_path or str(settings.storage.resolved_path / "notes")
        enc_key = _state.get("_encryption_key") if settings.encryption.files else None
        vault = ObsidianVault(vault_path, encryption_key=enc_key)

        enrichment_svc = NoteEnrichmentService(db, llm_router)
        relation_svc = NoteRelationService(db, vector_store)
        projection_svc = NoteProjectionService(db, vector_store, vault)

        note_capture = NoteCaptureService(db)
        note_processor = NoteProcessor(db, enrichment_svc, relation_svc, projection_svc)
        note_capture.set_processor(note_processor)

        _state["note_capture"] = note_capture
        _state["note_processor"] = note_processor
        _state["note_vault"] = vault
        # Legacy compat — some code still checks note_agent
        _state["note_agent"] = note_processor
        logger.info("Smart Notes v1.5 initialized (capture → enrich → relate → project)")

    tg_app = None
    if settings.telegram.bot_token:
        from telegram.ext import Application as TgApp
        from app.bot.handlers import BotHandlers

        tg_app = TgApp.builder().token(settings.telegram.bot_token).build()
        bot_handlers = BotHandlers(pipeline, search_fn=llm_search.answer, analytics_fn=llm_analytics.analyze)
        bot_handlers.register(tg_app)
        await tg_app.initialize()
        await tg_app.bot.set_my_commands(bot_handlers.COMMANDS)
        await tg_app.start()

        if settings.telegram.webhook_url:
            # Webhook mode — Telegram pushes updates to our HTTPS endpoint
            webhook_url = settings.telegram.webhook_url
            secret = settings.telegram.webhook_secret
            await tg_app.bot.set_webhook(
                url=webhook_url,
                secret_token=secret or None,
            )
            logger.info(f"Telegram bot started (webhook: {webhook_url})")
        else:
            # Polling mode — we pull updates from Telegram
            await tg_app.updater.start_polling(poll_interval=1)
            logger.info("Telegram bot started (polling)")

        _state["tg_app"] = tg_app
    else:
        logger.warning("TELEGRAM_BOT_TOKEN not set — bot disabled")

    reload_task = None
    if settings.skills.auto_reload:
        reload_task = asyncio.create_task(
            _skill_reload_loop(skill_engine, settings.skills.reload_interval_seconds)
        )

    cleanup_task = asyncio.create_task(
        _orphan_cleanup_loop(db, vector_store, interval=300)
    )
    reminder_task = asyncio.create_task(
        _reminder_loop(db, tg_app, settings.telegram.bot_token, interval=3600)
    )
    note_reminder_task = asyncio.create_task(
        _note_reminder_loop(db, tg_app, interval=300)
    )
    advice_task = asyncio.create_task(
        _daily_advice_loop(insights_engine, morning_engine, tg_app)
    )
    anomaly_task = asyncio.create_task(
        _anomaly_check_loop(db, tg_app, interval=14400)
    )

    note_processing_task = None
    checkin_task = None
    daily_summary_task = None
    weekly_report_task = None
    if note_processor and settings.notes.enabled:
        # v1.5: NoteProcessor runs its own loop (queue + DB scan)
        note_processor.tg_app = tg_app
        note_processing_task = asyncio.create_task(note_processor.run())
        if settings.notes.checkin_enabled:
            checkin_task = asyncio.create_task(
                _evening_checkin_loop(note_capture, db, tg_app, settings)
            )
        daily_summary_task = asyncio.create_task(
            _daily_summary_note_loop(note_processor, db, tg_app)
        )
        weekly_report_task = asyncio.create_task(
            _weekly_report_loop(note_processor, db, llm_router, tg_app)
        )
    # Inbox file watcher
    inbox_watcher_task = None
    if note_capture and settings.notes.enabled and settings.notes.inbox_watch_enabled:
        vault_base = settings.notes.vault_path or str(settings.storage.resolved_path / "notes")
        inbox_p = settings.notes.inbox_path or f"{vault_base}/_inbox"
        archive_p = settings.notes.archive_path or f"{vault_base}/_archive"
        from app.notes.watcher import InboxWatcher
        watcher = InboxWatcher(inbox_p, archive_p, db, note_capture)
        inbox_watcher_task = asyncio.create_task(
            watcher.watch_loop(settings.notes.inbox_watch_interval)
        )

    # Initialize MCP streamable HTTP session manager
    from app.mcp_server import mcp as _mcp_server
    _streamable_app = _mcp_server.streamable_http_app()
    _mcp_cm = _mcp_server.session_manager.run()
    await _mcp_cm.__aenter__()
    _state["_mcp_cm"] = _mcp_cm

    logger.info("All systems initialized")
    yield

    logger.info("Shutting down...")
    # Close MCP session manager
    if _state.get("_mcp_cm"):
        try:
            await _state["_mcp_cm"].__aexit__(None, None, None)
        except Exception:
            pass
    for t in [cleanup_task, reminder_task, note_reminder_task, anomaly_task,
              note_processing_task, checkin_task, daily_summary_task, weekly_report_task,
              inbox_watcher_task]:
        if t:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
    if reload_task:
        reload_task.cancel()
        try:
            await reload_task
        except asyncio.CancelledError:
            pass
    if tg_app:
        if tg_app.updater and tg_app.updater.running:
            await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
    await vector_store.close()
    await db.close()
    logger.info("Shutdown complete")


async def _orphan_cleanup_loop(db, vector_store, interval: int = 300):
    """Periodically clean up orphans in both directions:
    1. DB records whose files are missing from disk → delete DB + Qdrant
    2. Disk files with no DB record → delete from disk
    """
    from app.config import get_settings
    base_path = get_settings().storage.resolved_path

    while True:
        await asyncio.sleep(interval)
        try:
            removed_db = 0
            removed_disk = 0

            # Direction 1: DB → disk (file record exists but file is gone)
            files = await db.list_file_paths()
            known_paths = set()
            for f in files:
                stored_path = f.get("stored_path", "")
                known_paths.add(stored_path)
                file_storage = get_state("file_storage")
                is_missing = False
                if file_storage:
                    try:
                        is_missing = stored_path and not await file_storage.exists(stored_path)
                    except Exception:
                        is_missing = False
                else:
                    is_missing = stored_path and not Path(stored_path).exists()
                if is_missing:
                    file_id = f["id"]
                    lifecycle = get_state("lifecycle")
                    if lifecycle:
                        await lifecycle.delete(file_id)
                    else:
                        try:
                            await vector_store.delete_document(file_id)
                        except Exception:
                            pass
                        await db.delete_file(file_id)
                    removed_db += 1

            # Direction 2: disk → DB (file on disk but no DB record)
            # Skip notes/ directory — notes are managed by NoteAgent, not file pipeline
            skip_dirs = {"notes"}
            if base_path.exists():
                for disk_file in base_path.rglob("*"):
                    if disk_file.is_file() and str(disk_file) not in known_paths:
                        # Don't delete files in excluded directories
                        try:
                            rel = disk_file.relative_to(base_path)
                            if rel.parts and rel.parts[0] in skip_dirs:
                                continue
                        except ValueError:
                            pass
                        disk_file.unlink()
                        removed_disk += 1

            # Direction 3: Notes — DB record exists but vault .md is gone → delete from DB
            removed_notes = 0
            try:
                cursor = await db.db.execute(
                    "SELECT id, vault_path FROM notes WHERE vault_path != '' AND vault_path IS NOT NULL"
                )
                for row in await cursor.fetchall():
                    note_id, vault_path = row[0], row[1]
                    if vault_path and not Path(vault_path).exists():
                        await db.delete_note(note_id)
                        removed_notes += 1
            except Exception as e:
                logger.debug(f"Note orphan check error: {e}")

            if removed_db or removed_disk or removed_notes:
                parts = []
                if removed_db:
                    parts.append(f"{removed_db} DB orphan(s)")
                if removed_disk:
                    parts.append(f"{removed_disk} disk orphan(s)")
                if removed_notes:
                    parts.append(f"{removed_notes} note orphan(s)")
                logger.info(f"Orphan cleanup: {', '.join(parts)} removed")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Orphan cleanup error: {e}")


async def _reminder_loop(db, tg_app, bot_token: str, interval: int = 3600):
    """Check for due reminders and send Telegram notifications."""
    while True:
        await asyncio.sleep(interval)
        try:
            if not db:
                continue
            due = await db.get_due_reminders()
            if not due:
                continue

            from app.bot.handlers import get_owner_chat_id_async
            chat_id = await get_owner_chat_id_async(db)

            for r in due:
                try:
                    text = (
                        f"⏰ Напоминание!\n\n"
                        f"📄 {r.get('original_name', 'Файл')}\n"
                        f"📁 {r.get('category', '')}\n"
                        f"📝 {r.get('message', 'Срок действия документа истекает')}"
                    )

                    if chat_id and tg_app:
                        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                        keyboard = InlineKeyboardMarkup([
                            [
                                InlineKeyboardButton("✅ Готово", callback_data=f"rem:done:{r['id']}"),
                                InlineKeyboardButton("⏰ +1 день", callback_data=f"rem:snooze:{r['id']}"),
                            ]
                        ])
                        await tg_app.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
                        logger.info(f"Reminder sent to chat {chat_id}: {r['id']}")
                    else:
                        logger.info(f"REMINDER DUE (no chat_id): {text}")

                    await db.mark_reminder_sent(r["id"])
                except Exception as e:
                    logger.warning(f"Failed to send reminder {r['id']}: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Reminder loop error: {e}")


async def _note_reminder_loop(db, tg_app, interval: int = 3600):
    """Check for due note reminders and send Telegram notifications."""
    await asyncio.sleep(120)  # initial delay
    while True:
        try:
            if not db:
                await asyncio.sleep(interval)
                continue
            due = await db.get_due_note_reminders()
            if not due:
                await asyncio.sleep(interval)
                continue

            from app.bot.handlers import get_owner_chat_id_async
            chat_id = await get_owner_chat_id_async(db)

            for r in due:
                try:
                    text = f"⏰ Напоминание\n\n📝 {r['description']}"
                    if r.get("user_title"):
                        text += f"\n📎 Заметка: {r['user_title']}"

                    if chat_id and tg_app:
                        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
                        keyboard = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Готово", callback_data=f"nrem:done:{r['id']}"),
                            InlineKeyboardButton("⏰ +1 день", callback_data=f"nrem:snooze:{r['id']}"),
                        ]])
                        await tg_app.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
                        logger.info(f"Note reminder sent: #{r['id']} for note #{r['note_id']}")
                    else:
                        logger.info(f"NOTE REMINDER DUE (no chat_id): {text}")

                    await db.mark_note_reminder_sent(r["id"])

                    # Create next occurrence for recurring reminders
                    rule = r.get("recurrence_rule", "")
                    if rule:
                        from app.notes.reminders import compute_next_occurrence
                        next_at = compute_next_occurrence(
                            r["remind_at"], rule, r.get("recurrence_end", ""),
                        )
                        if next_at:
                            await db.create_note_reminder(
                                note_id=r["note_id"],
                                description=r["description"],
                                remind_at=next_at,
                                task_id=r.get("task_id"),
                                source="recurring",
                                recurrence_rule=rule,
                                recurrence_end=r.get("recurrence_end", ""),
                            )
                            logger.info(f"Recurring reminder scheduled: next at {next_at}")
                except Exception as e:
                    logger.warning(f"Failed to send note reminder {r['id']}: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Note reminder loop error: {e}")
        await asyncio.sleep(interval)


async def _anomaly_check_loop(db, tg_app, interval: int = 14400):
    """Check for anomalies every 4 hours and send alerts."""
    await asyncio.sleep(300)  # initial delay
    while True:
        try:
            from app.notes.anomaly import AnomalyDetector
            detector = AnomalyDetector(db)
            alerts = await detector.check_anomalies()

            if alerts and tg_app:
                from app.bot.handlers import get_owner_chat_id_async
                chat_id = await get_owner_chat_id_async(db)
                if chat_id:
                    severity_icons = {"critical": "🚨", "warning": "⚠️"}
                    for a in alerts:
                        icon = severity_icons.get(a.severity, "ℹ️")
                        text = f"{icon} {a.message}"
                        if a.context:
                            text += f"\n\n💡 {a.context}"
                        await tg_app.bot.send_message(chat_id=chat_id, text=text)
                        logger.info(f"Anomaly alert sent: {a.alert_type}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Anomaly check error: {e}")
        await asyncio.sleep(interval)


async def _skill_reload_loop(skill_engine, interval: int):
    while True:
        await asyncio.sleep(interval)
        try:
            changed = await skill_engine.reload_changed()
            if changed:
                logger.info(f"Skills reloaded: {changed}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Skill reload error: {e}")


async def _daily_advice_loop(insights_engine, morning_engine, tg_app):
    """Send morning briefing at 9:00 and evening advice at 20:00."""
    from datetime import datetime, timedelta
    from app.bot.handlers import get_owner_chat_id_async

    while True:
        try:
            now = datetime.now()
            # Find next 9:00 or 20:00
            today_9 = now.replace(hour=9, minute=0, second=0, microsecond=0)
            today_20 = now.replace(hour=20, minute=0, second=0, microsecond=0)

            targets = []
            for t, tod in [(today_9, "morning"), (today_20, "evening")]:
                if t > now:
                    targets.append((t, tod))
                else:
                    targets.append((t + timedelta(days=1), tod))

            next_target, time_of_day = min(targets, key=lambda x: x[0])
            wait_seconds = (next_target - now).total_seconds()
            logger.info(f"Daily advice: next at {next_target.strftime('%H:%M')} ({time_of_day}), waiting {wait_seconds/3600:.1f}h")

            await asyncio.sleep(wait_seconds)

            chat_id = await get_owner_chat_id_async()
            if not chat_id or not tg_app:
                continue

            if time_of_day == "morning" and morning_engine:
                # Morning: note-centric personalized briefing
                brief = await morning_engine.generate_morning_brief()
                text = morning_engine.format_telegram_brief(brief)
                if text:
                    await tg_app.bot.send_message(chat_id=chat_id, text=text)
                    logger.info("Morning briefing sent")
            elif insights_engine:
                # Evening: document-insights advice (existing behavior)
                advice = await insights_engine.generate_daily_advice(time_of_day)
                if advice:
                    await tg_app.bot.send_message(chat_id=chat_id, text=advice)
                    logger.info("Evening advice sent")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Daily advice error: {e}")
            await asyncio.sleep(3600)


async def _note_processing_loop(note_agent, interval: int = 900):
    """Process unprocessed notes periodically."""
    await asyncio.sleep(30)  # initial delay
    while True:
        try:
            processed = await note_agent.process_unprocessed()
            if processed:
                logger.info(f"Note agent processed {processed} notes")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Note processing error: {e}")
        await asyncio.sleep(interval)


async def _evening_checkin_loop(note_capture, db, tg_app, settings):
    """Run evening check-in at configured hour."""
    from datetime import datetime, timedelta
    from app.bot.handlers import get_owner_chat_id_async
    from app.notes.checkin import EveningCheckin

    while True:
        try:
            now = datetime.now()
            target_hour = settings.notes.checkin_hour
            target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_secs = (target - now).total_seconds()
            logger.info(f"Evening check-in: next at {target.strftime('%H:%M')}, waiting {wait_secs/3600:.1f}h")
            await asyncio.sleep(wait_secs)

            if not tg_app:
                continue

            chat_id = await get_owner_chat_id_async(db)
            if not chat_id:
                continue

            # Send web checkin link
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            public_url = ""
            if settings.telegram.webhook_url:
                from urllib.parse import urlparse
                parsed = urlparse(settings.telegram.webhook_url)
                public_url = f"{parsed.scheme}://{parsed.netloc}"
            else:
                host = settings.web.host if settings.web.host != "0.0.0.0" else "localhost"
                public_url = f"http://{host}:{settings.web.port}"

            checkin_url = f"{public_url}/notes/checkin"
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📝 Открыть чекин", url=checkin_url),
            ]])
            await tg_app.bot.send_message(
                chat_id=chat_id,
                text="🌙 Время вечернего чекина!\n\nЗаполни настроение, сон, еду и другие сигналы дня.",
                reply_markup=keyboard,
            )
            logger.info("Evening check-in link sent")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Evening checkin error: {e}")
            await asyncio.sleep(3600)


async def _daily_summary_note_loop(note_agent, db, tg_app):
    """Generate daily summary at 23:55 and send to Telegram."""
    from datetime import datetime, timedelta
    from app.bot.handlers import get_owner_chat_id_async

    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=23, minute=55, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait_secs = (target - now).total_seconds()
            await asyncio.sleep(wait_secs)

            today = datetime.now().strftime("%Y-%m-%d")

            # Generate daily MOC via vault
            vault = get_state("note_vault")
            if vault:
                try:
                    day_notes = await db.get_daily_notes(today)
                    day_facts = await db.get_daily_facts(today)
                    moc_metrics = {}
                    if "calories" in day_facts:
                        moc_metrics["calories_total"] = day_facts["calories"]["total"]
                    if "mood_score" in day_facts:
                        moc_metrics["mood_avg"] = round(day_facts["mood_score"]["avg"], 1)
                    if "weight_kg" in day_facts:
                        moc_metrics["weight"] = day_facts["weight_kg"]["avg"]
                    vault.update_daily_moc(today, day_notes, moc_metrics)
                except Exception as e:
                    logger.debug(f"Daily MOC generation failed: {e}")

            # Send Telegram summary
            if tg_app:
                chat_id = await get_owner_chat_id_async(db)
                if chat_id:
                    notes = await db.get_daily_notes(today)
                    metrics = await db.get_daily_facts(today)
                    streak = await db.get_streak()

                    parts = [f"📊 Итоги дня ({today})", ""]
                    parts.append(f"📝 Заметок: {len(notes)}")
                    if metrics.get("calories"):
                        parts.append(f"🍽 Калории: ~{int(metrics['calories']['total'])} kcal")
                    if metrics.get("mood_score"):
                        parts.append(f"💭 Настроение: {metrics['mood_score']['avg']:.1f}/10")
                    if metrics.get("energy"):
                        parts.append(f"⚡ Энергия: {metrics['energy']['avg']:.1f}/10")
                    if metrics.get("weight_kg"):
                        parts.append(f"⚖️ Вес: {metrics['weight_kg']['avg']:.1f} кг")
                    if metrics.get("sleep_hours"):
                        parts.append(f"😴 Сон: {metrics['sleep_hours']['total']:.1f}ч")
                    if streak > 1:
                        parts.append(f"🔥 Стрик: {streak} дней подряд!")
                    parts.append("\nСпокойной ночи! 🌙")

                    await tg_app.bot.send_message(chat_id=chat_id, text="\n".join(parts))
                    logger.info(f"Daily summary sent for {today}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Daily summary error: {e}")
            await asyncio.sleep(3600)


async def _weekly_report_loop(note_agent, db, llm_router, tg_app):
    """Generate weekly report every Sunday at 22:00."""
    from datetime import datetime, timedelta
    from app.bot.handlers import get_owner_chat_id_async
    from app.notes.weekly import WeeklyAnalyzer

    while True:
        try:
            now = datetime.now()
            # Find next Sunday 22:00
            days_until_sunday = (6 - now.weekday()) % 7
            if days_until_sunday == 0 and now.hour >= 22:
                days_until_sunday = 7
            target = (now + timedelta(days=days_until_sunday)).replace(
                hour=22, minute=0, second=0, microsecond=0
            )
            wait_secs = (target - now).total_seconds()
            logger.info(f"Weekly report: next Sunday at 22:00, waiting {wait_secs/3600:.1f}h")
            await asyncio.sleep(wait_secs)

            vault = get_state("note_vault")
            analyzer = WeeklyAnalyzer(db, llm_router, vault=vault)
            summary = await analyzer.get_weekly_telegram_summary()

            if tg_app and summary:
                chat_id = await get_owner_chat_id_async(db)
                if chat_id:
                    await tg_app.bot.send_message(chat_id=chat_id, text=summary)
                    logger.info("Weekly report sent")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Weekly report error: {e}")
            await asyncio.sleep(3600)


app = FastAPI(title="Smart Storage", version="0.2.0", lifespan=lifespan)

# ── Security Middleware ──────────────────────────────────────────────────────
import secrets as _secrets
from starlette.middleware.sessions import SessionMiddleware
from fastapi.middleware.cors import CORSMiddleware
from app.web.auth import AuthMiddleware
from app.web.csrf import CSRFMiddleware

_settings = get_settings()


# Security headers middleware
class SecurityHeadersMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                # Skip CSP for file download responses (PDF viewer needs full permissions)
                path = scope.get("path", "")
                if "/download" in path or path.startswith("/mcp") or path.startswith("/api"):
                    await send(message)
                    return
                headers = dict(message.get("headers", []))
                extra = [
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                    (b"content-security-policy",
                     b"default-src 'self'; "
                     b"script-src 'self' https://unpkg.com 'unsafe-inline' 'unsafe-eval'; "
                     b"style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; "
                     b"font-src 'self' https://fonts.gstatic.com; "
                     b"img-src 'self' data:; "
                     b"connect-src 'self' https://unpkg.com; "
                     b"frame-src 'self'; "
                     b"object-src 'self'"),
                ]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)

# CSRF middleware (validates token on POST/PUT/PATCH/DELETE)
app.add_middleware(CSRFMiddleware)

# Auth middleware (checks session cookie)
app.add_middleware(AuthMiddleware)

# Session middleware (must be added after AuthMiddleware — Starlette runs in reverse)
if not _settings.web.session_secret:
    logging.getLogger(__name__).warning(
        "WEB__SESSION_SECRET not set — sessions will NOT persist across restarts. "
        "Set WEB__SESSION_SECRET in .env for stable sessions."
    )
_session_secret = _settings.web.session_secret or _secrets.token_urlsafe(32)
app.add_middleware(SessionMiddleware, secret_key=_session_secret, max_age=604800)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.web.cors_origins,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=True,
)

# Rate limiting
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.web.limiter import limiter as _limiter
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Include web UI routes
from app.web.routes import router as web_router
app.include_router(web_router)

# Include REST API v1
from app.api.routes import router as api_router
app.include_router(api_router)

# Favicon (suppress 404)
from fastapi.responses import Response as _Resp

@app.get("/favicon.ico")
async def favicon():
    return _Resp(
        content=(
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<text y=".9em" font-size="90">📁</text></svg>'
        ),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )

# Mount MCP server (both transports)
from app.mcp_server import mcp
# Static files (CSS, JS)
from starlette.staticfiles import StaticFiles as _StaticFiles
_static_dir = Path(__file__).parent / "web" / "static"
if _static_dir.exists():
    app.mount("/static", _StaticFiles(directory=str(_static_dir)), name="static")

# Mount MCP server (both transports)
from app.mcp_server import mcp as _mcp
app.mount("/mcp/sse", _mcp.sse_app())                # Legacy SSE transport
app.mount("/mcp", _mcp.streamable_http_app())         # Streamable HTTP (Codex, Claude Code)


# Telegram webhook endpoint
@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Receive Telegram updates via webhook."""
    from fastapi import Header, HTTPException
    tg_app = get_state("tg_app")
    if not tg_app:
        raise HTTPException(status_code=503, detail="Bot not running")

    # Verify secret token if configured
    secret = _settings.telegram.webhook_secret
    if secret:
        token = request.headers.get("x-telegram-bot-api-secret-token", "")
        if token != secret:
            raise HTTPException(status_code=403, detail="Invalid secret")

    from telegram import Update as TgUpdate
    data = await request.json()
    update = TgUpdate.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


# Global exception handler — prevent stack trace leaks
from fastapi.responses import JSONResponse as _JSONResp

@app.exception_handler(Exception)
async def _global_error_handler(request, exc):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return _JSONResp(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
async def health():
    vs = get_state("vector_store")
    vector_health = await vs.health_check() if vs else {}
    db = get_state("db")
    return {
        "status": "ok" if db and db._db else "degraded",
        "version": "0.1.0",
        "database": "connected" if db and db._db else "disconnected",
        "qdrant": vector_health,
        "telegram": "running" if get_state("tg_app") else "disabled",
        "skills": len(get_state("skill_engine").list_skills()) if get_state("skill_engine") else 0,
    }


# Legacy /api/* endpoints REMOVED — use authenticated /api/v1/* instead
