"""AI File Intelligence Agent — Entry Point with full lifespan management."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

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
    logger.info("Starting AI File Intelligence Agent v0.1.0")

    from app.storage.db import Database
    db = Database(settings.database.path)
    await db.connect()
    _state["db"] = db
    logger.info(f"Database ready: {settings.database.path}")

    # Load encrypted secrets from DB → environment (supplement .env)
    if settings.web.session_secret:
        await _load_secrets_to_env(db, settings.web.session_secret)
    else:
        logger.warning("WEB__SESSION_SECRET not set — encrypted secrets will NOT be loaded from DB")

    from app.storage.files import FileStorage
    file_storage = FileStorage(
        base_path=settings.storage.base_path,
        allowed_extensions=settings.storage.allowed_extensions,
    )
    _state["file_storage"] = file_storage

    # System key — used by pipeline._step_store to encrypt sensitive
    # documents on disk. Generated on first start, persisted in
    # data/.system_key (mode 0600). Never leaves the process.
    from app.utils.crypto import load_or_create_system_key
    try:
        _state["system_key"] = load_or_create_system_key()
        logger.info("System key loaded — sensitive document encryption available")
    except Exception as exc:
        logger.warning(
            f"Could not initialize system key ({exc}); sensitive uploads "
            "will fall back to plaintext"
        )
        _state["system_key"] = None

    from app.storage.vectors import VectorStore
    vector_store = VectorStore(settings.qdrant, settings.embedding, google_api_key=settings.google_api_key)
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

    # Cognee sidecar — must be probed before Pipeline so it can be injected.
    from app.memory import CogneeClient, DevIngestor
    cognee_client = CogneeClient(settings.cognee)
    await cognee_client.setup()
    _state["cognee"] = cognee_client
    _state["dev_ingestor"] = DevIngestor(db=db, cognee_client=cognee_client)
    if settings.cognee.enabled and not cognee_client.healthy:
        logger.warning(
            "Cognee sidecar not reachable at %s — memory features disabled until 'make cognee-start'",
            settings.cognee.base_url,
        )

    from app.pipeline import Pipeline
    pipeline = Pipeline(
        settings=settings, db=db, file_storage=file_storage,
        vector_store=vector_store, parser_factory=parser_factory,
        llm_router=llm_router, classifier=classifier, skill_engine=skill_engine,
        cognee_client=cognee_client,
    )
    _state["pipeline"] = pipeline

    from app.llm.search import LLMSearch
    llm_search = LLMSearch(vector_store, llm_router, db=db, cognee_client=cognee_client)
    _state["llm_search"] = llm_search

    from app.llm.analytics import LLMAnalytics
    llm_analytics = LLMAnalytics(vector_store, llm_router, db=db)
    _state["llm_analytics"] = llm_analytics

    from app.llm.insights import InsightsEngine
    insights_engine = InsightsEngine(llm_router, db)
    _state["insights_engine"] = insights_engine

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
        await tg_app.updater.start_polling(poll_interval=1)
        _state["tg_app"] = tg_app
        logger.info("Telegram bot started (polling)")
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
    advice_task = asyncio.create_task(
        _daily_advice_loop(insights_engine, tg_app)
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
    for t in [cleanup_task, reminder_task]:
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
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
    if _state.get("cognee"):
        try:
            await _state["cognee"].shutdown()
        except Exception:
            pass
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
                if stored_path and not Path(stored_path).exists():
                    file_id = f["id"]
                    try:
                        await vector_store.delete_document(file_id)
                    except Exception:
                        pass
                    await db.delete_file(file_id)
                    removed_db += 1

            # Direction 2: disk → DB (file on disk but no DB record)
            if base_path.exists():
                for disk_file in base_path.rglob("*"):
                    if disk_file.is_file() and str(disk_file) not in known_paths:
                        disk_file.unlink()
                        removed_disk += 1

            if removed_db or removed_disk:
                logger.info(
                    f"Orphan cleanup: {removed_db} DB orphan(s), "
                    f"{removed_disk} disk orphan(s) removed"
                )
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

            from app.bot.handlers import get_owner_chat_id
            chat_id = get_owner_chat_id()

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


async def _daily_advice_loop(insights_engine, tg_app):
    """Send daily motivational advice at 9:00 and 20:00."""
    from datetime import datetime, timedelta
    from app.bot.handlers import get_owner_chat_id

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

            # Generate and send advice
            chat_id = get_owner_chat_id()
            if chat_id and tg_app and insights_engine:
                advice = await insights_engine.generate_daily_advice(time_of_day)
                if advice:
                    await tg_app.bot.send_message(chat_id=chat_id, text=advice)
                    logger.info(f"Daily advice sent ({time_of_day})")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Daily advice error: {e}")
            await asyncio.sleep(3600)  # retry in 1 hour


app = FastAPI(title="AI File Intelligence Agent", version="0.1.0", lifespan=lifespan)

# ── Security Middleware ──────────────────────────────────────────────────────
import secrets as _secrets
from starlette.middleware.sessions import SessionMiddleware
from fastapi.middleware.cors import CORSMiddleware
from app.web.auth import AuthMiddleware

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
                headers = dict(message.get("headers", []))
                extra = [
                    (b"x-frame-options", b"DENY"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                ]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)

# Auth middleware (checks session cookie)
app.add_middleware(AuthMiddleware)

# Session middleware (must be added after AuthMiddleware — Starlette runs in reverse)
_session_secret = _settings.web.session_secret or _secrets.token_urlsafe(32)
app.add_middleware(SessionMiddleware, secret_key=_session_secret, max_age=604800)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://fag.n8nskorx.top"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
    allow_credentials=True,
)

# Rate limiting
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.web.routes import limiter as _limiter
app.state.limiter = _limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Include web UI routes
from app.web.routes import router as web_router
app.include_router(web_router)

# Include REST API v1
from app.api.routes import router as api_router
app.include_router(api_router)

# Mount MCP server (both transports)
from app.mcp_server import mcp
app.mount("/mcp/sse", mcp.sse_app())                # Legacy SSE transport
app.mount("/mcp", mcp.streamable_http_app())         # Streamable HTTP (Codex, Claude Code)


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
    cognee = get_state("cognee")
    return {
        "status": "ok" if db and db._db else "degraded",
        "version": "0.1.0",
        "database": "connected" if db and db._db else "disconnected",
        "qdrant": vector_health,
        "telegram": "running" if get_state("tg_app") else "disabled",
        "skills": len(get_state("skill_engine").list_skills()) if get_state("skill_engine") else 0,
        "cognee": "healthy" if cognee and cognee.healthy else "disabled",
    }


@app.get("/api/stats")
async def api_stats():
    db = get_state("db")
    return await db.get_stats() if db else {"error": "not initialized"}


@app.get("/api/files")
async def api_files(category: str | None = None, limit: int = 50, offset: int = 0):
    db = get_state("db")
    if not db:
        return {"error": "not initialized"}
    return {"files": await db.list_files(category=category, limit=limit, offset=offset)}


@app.get("/api/search")
async def api_search(q: str, top_k: int = 5):
    search = get_state("llm_search")
    if not search:
        return {"error": "not initialized"}
    result = await search.answer(q, top_k=top_k)
    return {"query": q, "answer": result.get("text", ""), "file_ids": list(result.get("file_ids", {}).keys())}
