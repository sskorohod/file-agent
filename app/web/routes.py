"""Web routes — dashboard, files, search, skills, settings, logs."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.web.csrf import get_csrf_token
from app.web.limiter import limiter

router = APIRouter()

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

# Override TemplateResponse to auto-inject csrf_token into every context
_original_template_response = templates.TemplateResponse


def _csrf_template_response(name, context, **kwargs):
    request = context.get("request")
    if request and "csrf_token" not in context:
        context["csrf_token"] = get_csrf_token(request)
    return _original_template_response(name, context, **kwargs)


templates.TemplateResponse = _csrf_template_response


def _get(key: str):
    from app.main import get_state
    return get_state(key)


def _safe_filename(name: str) -> str:
    """Escape filename for Content-Disposition header (RFC 5987)."""
    from urllib.parse import quote
    return quote(name, safe="")


# ── Auth ─────────────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login")
@limiter.limit("5/minute")
async def login(request: Request, login: str = Form(...), password: str = Form(...)):
    import logging
    import bcrypt
    from app.config import get_settings
    _log = logging.getLogger(__name__)
    settings = get_settings()
    login_ok = not settings.web.login or login.strip().lower() == settings.web.login.lower()
    password_ok = settings.web.password_hash and bcrypt.checkpw(
        password.encode(), settings.web.password_hash.encode()
    )
    _log.info(f"Login attempt: success={login_ok and password_ok}")
    if login_ok and password_ok:
        request.session.clear()  # Prevent session fixation
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html", {"request": request, "error": "Неверный логин или пароль"}, status_code=401
    )


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    from datetime import date
    db = _get("db")
    today = date.today().isoformat()
    stats = await db.get_stats() if db else {}
    recent = await db.list_files(limit=10) if db else []
    llm_session = _get("llm_router").get_stats() if _get("llm_router") else {}
    llm_all = await db.get_llm_stats() if db else {}
    llm_today = await db.get_llm_stats(since=today) if db else {}
    vs = _get("vector_store")
    qdrant_health = await vs.health_check() if vs else {}
    # New dashboard data
    total_queries = await db.get_total_queries() if db else 0
    today_queries = await db.get_total_queries(since=today) if db else 0
    processed = await db.get_processed_count() if db else 0
    errors = await db.get_error_count() if db else 0
    query_history = await db.get_query_history(limit=15) if db else []
    pipeline_health = await db.get_pipeline_health(limit=5) if db else []
    source_dist = await db.get_source_distribution() if db else {}
    # Determine provider mode: oauth (subscription) vs api (pay-per-token)
    from app.config import get_settings as _gs
    _search_cfg = _gs().llm.models.get("search")
    is_oauth = bool(_search_cfg and _search_cfg.api_base)

    # Reminders
    reminders_all = await db.list_reminders(include_sent=False) if db else []
    for r in reminders_all:
        try:
            meta = json.loads(r.get("metadata_json", "{}") or "{}")
            r["document_type"] = meta.get("document_type", "")
        except Exception:
            r["document_type"] = ""
    pending_reminders = reminders_all[:5]
    pending_count = len(reminders_all)

    # Build unified activity feed: searches + uploads, sorted by time
    activity = []
    for q in query_history:
        activity.append({"type": "search", "text": q.get("text", "?"), "source": q.get("source", "web"), "time": q.get("created_at", "")})
    for f in recent:
        activity.append({"type": "upload", "text": f["original_name"], "category": f.get("category", ""), "time": f.get("created_at", "")})
    activity.sort(key=lambda x: x.get("time", ""), reverse=True)
    activity = activity[:12]

    return templates.TemplateResponse("dashboard.html", {
        "request": request, "page": "dashboard",
        "stats": stats, "recent_files": recent,
        "llm_session": llm_session, "llm_all": llm_all, "llm_today": llm_today,
        "qdrant": qdrant_health,
        "qdrant_points": qdrant_health.get("points_count", 0),
        "total_queries": total_queries, "today_queries": today_queries,
        "processed": processed, "errors": errors,
        "query_history": query_history, "pipeline_health": pipeline_health,
        "source_dist": source_dist,
        "is_oauth": is_oauth,
        "activity": activity,
        "pending_reminders": pending_reminders, "pending_count": pending_count,
    })


@router.get("/files", response_class=HTMLResponse)
async def files_page(request: Request, category: str | None = None, q: str | None = None, page: int = 1, limit: int = 25):
    db = _get("db")
    offset = (page - 1) * limit
    if q:
        files = await db.search_files(q, limit=limit) if db else []
    else:
        files = await db.list_files(category=category, limit=limit, offset=offset) if db else []
    total = await db.count_files(category=category) if db else 0
    categories = list((await db.get_stats()).get("categories", {}).keys()) if db else []
    # Enrich files with document_type from metadata_json
    for f in files:
        try:
            meta = json.loads(f.get("metadata_json", "{}") or "{}")
            f["document_type"] = meta.get("document_type", "")
        except Exception:
            f["document_type"] = ""
    return templates.TemplateResponse("files.html", {
        "request": request, "page": "files", "files": files, "total": total,
        "current_page": page, "limit": limit, "category": category,
        "q": q or "", "categories": categories,
        "total_pages": max(1, (total + limit - 1) // limit),
    })


@router.get("/files/{file_id}", response_class=HTMLResponse)
async def file_detail(request: Request, file_id: str):
    db = _get("db")
    file = await db.get_file(file_id) if db else None
    log = await db.get_file_log(file_id) if db else []
    notes = await db.list_notes(file_id=file_id) if db else []
    if file:
        for key in ("tags", "metadata_json"):
            val = file.get(key)
            if isinstance(val, str):
                try:
                    file[key + "_parsed"] = json.loads(val)
                except Exception:
                    file[key + "_parsed"] = val
    return templates.TemplateResponse("file_detail.html", {
        "request": request, "page": "files", "file": file, "log": log, "notes": notes,
    })


@router.post("/files/reclassify-uncategorized")
async def reclassify_uncategorized(request: Request):
    """Reclassify all uncategorized files via LLM."""
    import logging
    log = logging.getLogger(__name__)
    db = _get("db")
    lifecycle = _get("lifecycle")
    if not db or not lifecycle:
        return RedirectResponse("/files", status_code=303)

    cursor = await db.db.execute(
        "SELECT id, extracted_text FROM files "
        "WHERE category='uncategorized' OR category='' OR category IS NULL"
    )
    files = [dict(r) for r in await cursor.fetchall()]
    count = 0
    for f in files:
        text = f.get("extracted_text", "") or ""
        if not text.strip():
            continue
        try:
            result = await lifecycle.reclassify(f["id"])
            if result:
                count += 1
                log.info(f"Reclassified {f['id'][:8]} → {result['category']}")
        except Exception as e:
            log.warning(f"Reclassify failed for {f['id'][:8]}: {e}")

    return RedirectResponse(f"/files?reclassified={count}", status_code=303)


@router.post("/files/{file_id}/reclassify")
async def reclassify_file(file_id: str):
    """Reclassify a single file via LLM."""
    import logging
    log = logging.getLogger(__name__)
    lifecycle = _get("lifecycle")
    if not lifecycle:
        return RedirectResponse(f"/files/{file_id}", status_code=303)

    try:
        result = await lifecycle.reclassify(file_id)
        if result:
            log.info(f"Reclassified {file_id[:8]} → {result['category']}")
    except Exception as e:
        log.warning(f"Reclassify failed: {e}")

    return RedirectResponse(f"/files/{file_id}", status_code=303)


@router.get("/files/{file_id}/download")
async def file_download(file_id: str, inline: bool = False):
    """Download or view the stored file. ?inline=true for in-browser preview."""
    db = _get("db")
    file = await db.get_file(file_id) if db else None
    if not file:
        return HTMLResponse("File not found", status_code=404)
    stored_uri = file["stored_path"]
    file_storage = _get("file_storage")
    if not file_storage or not await file_storage.exists(stored_uri):
        return HTMLResponse("File not found on disk", status_code=404)
    mime = file.get("mime_type", "application/octet-stream")
    previewable = mime in ("application/pdf",) or mime.startswith("image/")
    from starlette.responses import Response
    data = await file_storage.read_file(stored_uri)
    disposition = "inline" if (previewable or inline) else "attachment"
    return Response(
        content=data,
        media_type=mime,
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{_safe_filename(file['original_name'])}"},
    )


@router.post("/files/{file_id}/delete")
async def file_delete(request: Request, file_id: str):
    """Cascading delete via lifecycle service."""
    lifecycle = _get("lifecycle")
    if lifecycle:
        await lifecycle.delete(file_id)
    return RedirectResponse("/files", status_code=303)


@router.post("/files/upload")
@limiter.limit("10/minute")
async def file_upload(request: Request):
    """Upload file via web interface and process through pipeline."""
    import logging
    _log = logging.getLogger(__name__)

    form = await request.form()
    uploaded = form.get("file")
    if not uploaded or not hasattr(uploaded, "read"):
        return RedirectResponse("/files?error=no_file", status_code=303)

    filename = uploaded.filename or "upload"
    data = await uploaded.read()

    if not data:
        return RedirectResponse("/files?error=empty", status_code=303)

    pipeline = _get("pipeline")
    if not pipeline:
        return RedirectResponse("/files?error=unavailable", status_code=303)

    try:
        result = await pipeline.process(data, filename, source="web")
        if result.error:
            _log.warning(f"Web upload pipeline error: {result.error}")
            return RedirectResponse(f"/files?error={result.error[:100]}", status_code=303)
        if result.is_duplicate and result.duplicate_of:
            return RedirectResponse(f"/files/{result.duplicate_of.get('id', '')}", status_code=303)
        return RedirectResponse(f"/files/{result.file_id}", status_code=303)
    except Exception as e:
        _log.error(f"Web upload failed: {e}")
        return RedirectResponse("/files?error=upload_failed", status_code=303)


def _detect_category(query: str) -> str | None:
    """Simple keyword→category mapping for search filtering."""
    q = query.lower()
    mappings = {
        "personal": ["паспорт", "passport", "удостоверение", "свидетельство", "диплом", "виза", "id card"],
        "health": ["анализ", "кровь", "врач", "рецепт", "диагноз", "медицин", "health", "blood", "doctor"],
        "receipts": ["чек", "receipt", "квитанция", "оплата"],
        "business": ["счёт", "счет", "invoice", "договор", "contract", "налог", "tax"],
    }
    for category, keywords in mappings.items():
        if any(kw in q for kw in keywords):
            return category
    return None


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request, q: str | None = None):
    answer = None
    documents = []
    if q:
        vs = _get("vector_store")
        db = _get("db")
        results = []
        category_hint = _detect_category(q)

        if vs:
            # Try with category filter first
            if category_hint:
                results = await vs.search(q, top_k=20, category=category_hint)
            # Fallback to unfiltered if too few results
            if len(results) < 2:
                results = await vs.search(q, top_k=20)

        # Group chunks by file_id → document-level results
        if results and db:
            from collections import defaultdict
            import json as _json
            by_file = defaultdict(list)
            for r in results:
                by_file[r.file_id].append(r)

            for file_id, chunks in by_file.items():
                best = max(chunks, key=lambda c: c.score)
                if best.score < 0.50:
                    continue
                rec = await db.get_file(file_id)

                # Re-rank: boost if document_type matches query keywords
                score = best.score
                if rec:
                    try:
                        meta = _json.loads(rec.get("metadata_json", "{}") or "{}")
                        doc_type = (meta.get("document_type", "") or "").lower()
                        q_lower = q.lower()
                        # Boost matching document_type, penalize mismatches within same category
                        if doc_type and any(kw in doc_type for kw in q_lower.split()):
                            score = min(score + 0.10, 1.0)
                        elif category_hint and rec.get("category") == category_hint and doc_type:
                            score = score * 0.85  # penalize same-category but wrong type
                    except Exception:
                        pass

                documents.append({
                    "file_id": file_id,
                    "score": score,
                    "filename": rec.get("original_name", best.metadata.get("filename", "unknown")) if rec else best.metadata.get("filename", "unknown"),
                    "category": rec.get("category", best.metadata.get("category", "")) if rec else best.metadata.get("category", ""),
                    "summary": (rec.get("summary") or "") if rec else "",
                    "best_chunk_text": best.text,
                })
            documents.sort(key=lambda d: d["score"], reverse=True)

        search_mod = _get("llm_search")
        if search_mod and documents:
            # Pass already-found documents to avoid double vector search
            answer = await search_mod.answer(q, top_k=10)

    return templates.TemplateResponse("search.html", {
        "request": request, "page": "search", "q": q or "",
        "answer": answer, "documents": documents,
    })


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request, q: str | None = None):
    result = None
    chart_b64 = None
    if q:
        analytics = _get("llm_analytics")
        if analytics:
            result = await analytics.analyze(q)
            if result and result.chart_png:
                import base64
                chart_b64 = base64.b64encode(result.chart_png).decode()
    return templates.TemplateResponse("analytics.html", {
        "request": request, "page": "analytics", "q": q or "",
        "result": result, "chart_b64": chart_b64,
    })


@router.get("/skills", response_class=HTMLResponse)
async def skills_page(request: Request):
    engine = _get("skill_engine")
    skills = engine.list_skills() if engine else []
    return templates.TemplateResponse("skills.html", {
        "request": request, "page": "skills", "skills": skills,
    })


@router.get("/skills/{skill_name}", response_class=HTMLResponse)
async def skill_detail(request: Request, skill_name: str):
    engine = _get("skill_engine")
    skill = engine.get_skill(skill_name) if engine else None
    yaml_content = ""
    if skill:
        import yaml
        yaml_content = yaml.dump(skill.model_dump(exclude_defaults=False), default_flow_style=False, allow_unicode=True, sort_keys=False)
    elif skill_name in ("TEMPLATE", "new") and engine:
        # Load TEMPLATE.yaml directly for new skill creation
        template_path = Path(engine.skills_dir) / "TEMPLATE.yaml"
        if template_path.exists():
            yaml_content = template_path.read_text()
    return templates.TemplateResponse("skill_edit.html", {
        "request": request, "page": "skills", "skill": skill, "yaml_content": yaml_content,
    })


@router.post("/skills/{skill_name}/save", response_class=HTMLResponse)
async def skill_save(request: Request, skill_name: str, yaml_content: str = Form(...)):
    engine = _get("skill_engine")
    import yaml as yaml_lib
    message = error = None
    try:
        data = yaml_lib.safe_load(yaml_content)
        from app.skills.engine import SkillDefinition
        skill = SkillDefinition(**data)
        await engine.save_skill(skill)
        message = "Скилл сохранён"
    except Exception as e:
        error = str(e)
        skill = engine.get_skill(skill_name) if engine else None
    return templates.TemplateResponse("skill_edit.html", {
        "request": request, "page": "skills", "skill": skill,
        "yaml_content": yaml_content, "message": message, "error": error,
    })


def _settings_ctx(request: Request, tab: str, **extra) -> dict:
    """Common context for all settings tabs."""
    from app.config import get_settings
    settings = get_settings()
    saved = request.query_params.get("saved") or request.query_params.get("storage") == "saved"
    error = request.query_params.get("error")
    return {"request": request, "page": "settings", "tab": tab,
            "settings": settings, "saved": saved, "error": error, **extra}


@router.get("/settings", response_class=HTMLResponse)
async def settings_redirect(request: Request):
    return RedirectResponse("/settings/keys", status_code=303)


@router.get("/settings/keys", response_class=HTMLResponse)
async def settings_keys(request: Request):
    from app.utils.crypto import mask_key
    import os
    settings = _settings_ctx(request, "keys")["settings"]
    db = _get("db")
    api_keys = await db.list_api_keys() if db else []
    provider_keys = {
        "anthropic": mask_key(os.environ.get("ANTHROPIC_API_KEY", "")),
        "openai": mask_key(os.environ.get("OPENAI_API_KEY", "")),
        "google": mask_key(os.environ.get("GOOGLE_API_KEY", "")),
        "qdrant": mask_key(os.environ.get("QDRANT__API_KEY", settings.qdrant.api_key or "")),
    }
    new_key = request.query_params.get("new_key")
    return templates.TemplateResponse("settings_keys.html", {
        **_settings_ctx(request, "keys"),
        "api_keys": api_keys, "provider_keys": provider_keys, "new_key": new_key,
    })


@router.get("/settings/llm", response_class=HTMLResponse)
async def settings_llm_page(request: Request):
    from app.config import get_settings
    settings = get_settings()
    # Determine provider mode from first model's api_base
    first_model = next(iter(settings.llm.models.values()), None)
    oauth_base = first_model.api_base if first_model and first_model.api_base else ""
    provider_mode = "oauth" if oauth_base else "api"
    model_configs = {
        role: {"model": mc.model, "max_tokens": mc.max_tokens, "temperature": mc.temperature}
        for role, mc in settings.llm.models.items()
    }
    # Fetch available models (from proxy or presets)
    available_models = [
        "anthropic/claude-sonnet-4-20250514",
        "anthropic/claude-3-haiku-20240307",
        "openai/gpt-4.1",
        "openai/gpt-4.1-mini",
        "gemini/gemini-2.0-flash",
    ]
    if oauth_base:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{oauth_base}/models")
                if resp.status_code == 200:
                    data = resp.json()
                    available_models = [m["id"] for m in data.get("data", [])]
        except Exception:
            pass
    return templates.TemplateResponse("settings_llm.html", {
        **_settings_ctx(request, "llm"),
        "provider_mode": provider_mode, "oauth_base": oauth_base,
        "model_configs": model_configs,
        "available_models": available_models,
    })


@router.get("/settings/storage", response_class=HTMLResponse)
async def settings_storage_page(request: Request):
    return templates.TemplateResponse("settings_storage.html",
                                      _settings_ctx(request, "storage"))


@router.get("/settings/security", response_class=HTMLResponse)
async def settings_security_page(request: Request):
    from app.utils.crypto import is_encryption_configured
    from app.main import _state
    vs = _get("vector_store")
    qdrant_health = await vs.health_check() if vs else {}
    return templates.TemplateResponse("settings_security.html", {
        **_settings_ctx(request, "security"),
        "enc_configured": is_encryption_configured(),
        "enc_unlocked": _state.get("_encryption_key") is not None,
        "recovery_exists": Path("data/RECOVERY_KEY.txt").exists(),
        "qdrant_health": qdrant_health,
    })


@router.get("/settings/advanced", response_class=HTMLResponse)
async def settings_advanced_page(request: Request):
    from app.config import get_settings
    settings = get_settings()
    skill_engine = _get("skill_engine")
    tg_app = _get("tg_app")
    # Extract domain from webhook URL or CORS for display
    wh_url = settings.telegram.webhook_url
    webhook_domain = ""
    if wh_url:
        webhook_domain = wh_url.replace("https://", "").replace("http://", "").split("/")[0]
    # Default domain from CORS origins (first HTTPS origin)
    default_domain = ""
    for origin in settings.web.cors_origins:
        d = origin.replace("https://", "").replace("http://", "").split("/")[0]
        if d and d != "localhost":
            default_domain = d
            break
    # Determine active mode (what's actually running now)
    tg_active_mode = "polling"
    tg_active_webhook = ""
    if tg_app and tg_app.updater and not tg_app.updater.running:
        # Updater not running = webhook mode
        tg_active_mode = "webhook"
        tg_active_webhook = wh_url
    elif wh_url and tg_app:
        # Check if webhook was set at startup
        try:
            info = await tg_app.bot.get_webhook_info()
            if info and info.url:
                tg_active_mode = "webhook"
                tg_active_webhook = info.url
        except Exception:
            pass

    # Config mode (what's in config.yaml — may differ if not restarted)
    tg_config_mode = "webhook" if wh_url else "polling"

    return templates.TemplateResponse("settings_advanced.html", {
        **_settings_ctx(request, "advanced"),
        "tg_running": tg_app is not None,
        "skills_count": len(skill_engine.list_skills()) if skill_engine else 0,
        "webhook_domain": webhook_domain,
        "default_domain": default_domain,
        "tg_active_mode": tg_active_mode,
        "tg_active_webhook": tg_active_webhook,
        "tg_config_mode": tg_config_mode,
    })


# ── Advanced Settings POST routes ─────────────────────────────────────────

@router.post("/settings/advanced/restart")
async def restart_server(request: Request):
    """Restart uvicorn by touching a watched file (triggers --reload)."""
    import asyncio

    async def _do_restart():
        await asyncio.sleep(0.5)
        # Touch main.py to trigger uvicorn --reload file watcher
        Path("app/main.py").touch()

    asyncio.ensure_future(_do_restart())
    return HTMLResponse(
        '<html><head><meta http-equiv="refresh" content="5;url=/settings/advanced">'
        '</head><body style="background:#09090b;color:#fafafa;font-family:sans-serif;'
        'display:flex;align-items:center;justify-content:center;height:100vh">'
        '<div style="text-align:center"><h2>Restarting...</h2>'
        '<p style="color:#71717a">Redirecting in 5 seconds</p></div>'
        '</body></html>'
    )


def _save_yaml_config(updates: dict):
    """Merge updates into config.yaml and write."""
    import yaml as yaml_lib
    from app.config import reload_settings
    config_path = Path("config.yaml")
    config = yaml_lib.safe_load(config_path.read_text()) if config_path.exists() else {}

    for section, values in updates.items():
        config.setdefault(section, {})
        config[section].update(values)

    config_path.write_text(yaml_lib.dump(
        config, default_flow_style=False, allow_unicode=True, sort_keys=False,
    ))
    reload_settings()


@router.post("/settings/advanced/telegram")
async def save_telegram_settings(request: Request):
    """Save Telegram bot settings to config.yaml."""
    form = await request.form()
    updates = {}

    from app.config import get_settings as _gs_tg
    current = _gs_tg().telegram

    owner_id = form.get("owner_id", "")
    if owner_id:
        updates["owner_id"] = int(owner_id)

    # Webhook: only change if tg_mode radio is present in form
    tg_mode = form.get("tg_mode")
    if tg_mode == "webhook":
        domain = form.get("webhook_domain", "").strip().rstrip("/")
        if domain:
            import secrets as _sec
            updates["webhook_url"] = f"https://{domain}/telegram/webhook"
            if not current.webhook_secret:
                updates["webhook_secret"] = _sec.token_urlsafe(32)
            else:
                updates["webhook_secret"] = current.webhook_secret
    elif tg_mode == "polling":
        updates["webhook_url"] = ""
        updates["webhook_secret"] = ""
    # If tg_mode is None (not in form) — don't touch webhook settings

    auto_del = form.get("auto_delete_seconds", "")
    if auto_del:
        updates["auto_delete_seconds"] = int(auto_del)
    pin = form.get("pin_code")
    if pin is not None:
        updates["pin_code"] = pin
    tg_max = form.get("tg_max_file_size", "")
    if tg_max:
        updates["max_file_size_mb"] = int(tg_max)

    # Bot token → encrypted in DB (not config.yaml)
    bot_token = form.get("bot_token", "").strip()
    if bot_token:
        db = _get("db")
        from app.config import get_settings
        session_secret = get_settings().web.session_secret
        if db and session_secret:
            from app.utils.crypto import encrypt
            await db.set_secret("TELEGRAM_BOT_TOKEN", encrypt(bot_token, session_secret))

    if updates:
        _save_yaml_config({"telegram": updates})

    return RedirectResponse("/settings/advanced?saved=true", status_code=303)


@router.post("/settings/advanced/embedding")
async def save_embedding_settings(request: Request):
    """Save embedding & Qdrant settings to config.yaml."""
    form = await request.form()

    embedding_updates = {}
    for key, field in [
        ("provider", "embedding_provider"),
        ("model", "embedding_model"),
    ]:
        val = form.get(field, "")
        if val:
            embedding_updates[key] = val
    for key, field in [
        ("vector_size", "vector_size"),
        ("chunk_size_words", "chunk_size"),
        ("chunk_overlap_words", "chunk_overlap"),
    ]:
        val = form.get(field, "")
        if val:
            embedding_updates[key] = int(val)

    qdrant_updates = {}
    for key, field in [
        ("host", "qdrant_host"),
        ("collection_name", "qdrant_collection"),
    ]:
        val = form.get(field, "")
        if val:
            qdrant_updates[key] = val
    port = form.get("qdrant_port", "")
    if port:
        qdrant_updates["port"] = int(port)

    # Qdrant API key → encrypted in DB
    qdrant_key = form.get("qdrant_api_key", "").strip()
    if qdrant_key:
        db = _get("db")
        from app.config import get_settings
        session_secret = get_settings().web.session_secret
        if db and session_secret:
            from app.utils.crypto import encrypt
            await db.set_secret("QDRANT_API_KEY", encrypt(qdrant_key, session_secret))

    updates = {}
    if embedding_updates:
        updates["embedding"] = embedding_updates
    if qdrant_updates:
        updates["qdrant"] = qdrant_updates
    if updates:
        _save_yaml_config(updates)

    return RedirectResponse("/settings/advanced?saved=true", status_code=303)


@router.post("/settings/advanced/system")
async def save_system_settings(request: Request):
    """Save system settings to config.yaml."""
    form = await request.form()

    updates = {}

    log_level = form.get("log_level", "")
    if log_level:
        updates["logging"] = {"level": log_level}

    max_size = form.get("max_file_size", "")
    ext_str = form.get("allowed_extensions", "")
    skills_dir = form.get("skills_dir", "")

    storage_updates = {}
    if max_size:
        storage_updates["max_file_size_mb"] = int(max_size)
    if ext_str:
        exts = [e.strip() for e in ext_str.split(",") if e.strip()]
        storage_updates["allowed_extensions"] = exts
    if storage_updates:
        updates["storage"] = storage_updates

    if skills_dir:
        updates["skills"] = {"directory": skills_dir}

    if updates:
        _save_yaml_config(updates)

    return RedirectResponse("/settings/advanced?saved=true", status_code=303)


@router.post("/settings/keys/save")
async def save_provider_keys(request: Request):
    """Save provider API keys (encrypted in SQLite)."""
    import os
    from app.config import get_settings
    from app.utils.crypto import encrypt
    db = _get("db")
    session_secret = get_settings().web.session_secret
    if not session_secret:
        return RedirectResponse("/settings/keys", status_code=303)

    form = await request.form()
    KEY_MAP = {
        "anthropic_key": "ANTHROPIC_API_KEY",
        "openai_key": "OPENAI_API_KEY",
        "google_key": "GOOGLE_API_KEY",
        "qdrant_key": "QDRANT_API_KEY",
    }

    saved = []
    for field, secret_name in KEY_MAP.items():
        value = form.get(field, "").strip()
        if value and not value.startswith("***"):  # Skip masked placeholders
            encrypted = encrypt(value, session_secret)
            await db.set_secret(secret_name, encrypted)
            # Also update os.environ immediately
            env_name = "QDRANT__API_KEY" if secret_name == "QDRANT_API_KEY" else secret_name
            os.environ[env_name] = value
            saved.append(secret_name)

    # OAuth proxy URL
    oauth_url = form.get("oauth_proxy_url", "").strip()
    if oauth_url:
        encrypted = encrypt(oauth_url, session_secret)
        await db.set_secret("OAUTH_PROXY_URL", encrypted)
        saved.append("OAUTH_PROXY_URL")

    if saved:
        from app.config import reload_settings
        reload_settings()

    return RedirectResponse("/settings/keys?saved=true", status_code=303)


@router.post("/settings/prompts/save", response_class=HTMLResponse)
async def save_prompts(
    request: Request,
    search_prompt: str = Form(...),
    classification_prompt: str = Form(...),
):
    """Save system prompts to config.yaml and reload settings."""
    import yaml as yaml_lib
    from app.config import reload_settings

    config_path = Path("config.yaml")
    config = yaml_lib.safe_load(config_path.read_text()) if config_path.exists() else {}
    config.setdefault("llm", {})
    config["llm"]["search_prompt"] = search_prompt
    config["llm"]["classification_prompt"] = classification_prompt
    config_path.write_text(yaml_lib.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False))

    reload_settings()
    return RedirectResponse("/settings/llm?saved=true", status_code=303)


@router.get("/settings/llm/models", response_class=JSONResponse)
async def list_available_models(request: Request):
    """Fetch available models from OAuth proxy or return API model presets."""
    import httpx
    from app.config import get_settings
    settings = get_settings()

    # Try to fetch from OAuth proxy
    search_cfg = settings.llm.models.get("search")
    api_base = search_cfg.api_base if search_cfg and search_cfg.api_base else ""

    if api_base:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"{api_base}/models")
                if resp.status_code == 200:
                    data = resp.json()
                    models = [m["id"] for m in data.get("data", [])]
                    return JSONResponse({"source": "oauth", "models": models})
        except Exception:
            pass

    # Fallback: static presets
    return JSONResponse({"source": "presets", "models": [
        "anthropic/claude-sonnet-4-20250514",
        "anthropic/claude-3-haiku-20240307",
        "openai/gpt-4.1",
        "openai/gpt-4.1-mini",
        "gemini/gemini-2.0-flash",
    ]})


@router.post("/settings/llm/save", response_class=HTMLResponse)
async def save_llm_settings(request: Request):
    """Save LLM provider mode and model roles to config.yaml."""
    import yaml as yaml_lib
    from app.config import reload_settings

    form = await request.form()
    provider_mode = form.get("provider_mode", "oauth")  # "oauth" or "api"

    config_path = Path("config.yaml")
    config = yaml_lib.safe_load(config_path.read_text()) if config_path.exists() else {}
    config.setdefault("llm", {})
    config["llm"]["default_provider"] = "openai" if provider_mode == "oauth" else form.get("api_provider", "anthropic")
    config["llm"].setdefault("models", {})

    # Save OAuth proxy config
    oauth_base = ""
    if provider_mode == "oauth":
        oauth_base = form.get("oauth_base", "http://127.0.0.1:10531/v1")
        config["llm"]["oauth_proxy_url"] = oauth_base

        oauth_client_id = form.get("oauth_client_id", "").strip()
        if oauth_client_id:
            config["llm"]["oauth_client_id"] = oauth_client_id

        # OAuth secret → encrypted in DB
        oauth_secret = form.get("oauth_client_secret", "").strip()
        if oauth_secret:
            db = _get("db")
            from app.config import get_settings as _gs3
            session_secret = _gs3().web.session_secret
            if db and session_secret:
                from app.utils.crypto import encrypt as _enc
                await db.set_secret("OAUTH_CLIENT_SECRET", _enc(oauth_secret, session_secret))

    # Save per-role model config
    for role in ("classification", "extraction", "search", "analysis"):
        model = form.get(f"{role}_model", "")
        if not model:
            continue
        entry = {
            "model": f"openai/{model}" if provider_mode == "oauth" and "/" not in model else model,
            "max_tokens": int(form.get(f"{role}_max_tokens", 1024)),
            "temperature": float(form.get(f"{role}_temperature", 0.1)),
        }
        if provider_mode == "oauth":
            entry["api_base"] = oauth_base
            entry["api_key"] = "dummy"
        config["llm"]["models"][role] = entry

    config_path.write_text(yaml_lib.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False))
    reload_settings()
    return RedirectResponse("/settings/llm?saved=true", status_code=303)


@router.post("/settings/api-keys/create")
async def create_api_key(request: Request, name: str = Form("default"), mode: str = Form("lite")):
    db = _get("db")
    new_key = await db.create_api_key(name, mode=mode) if db else ""
    # Pass new_key via query param so it shows on the redirected page
    return RedirectResponse(f"/settings/keys?new_key={new_key}", status_code=303)


@router.post("/settings/api-keys/{key}/delete")
async def delete_api_key(key: str):
    db = _get("db")
    if db:
        await db.delete_api_key(key)
    return RedirectResponse("/settings/keys", status_code=303)



_SESSION_KEY_PATH = Path("data/.session_key")


def _save_session_key(key: bytes):
    """Save encryption key to temp file so it survives uvicorn reload.
    The key is encrypted with Fernet using the web session secret as password."""
    from cryptography.fernet import Fernet
    import hashlib, base64
    from app.config import get_settings
    secret = get_settings().web.session_secret
    if not secret:
        raise RuntimeError("WEB__SESSION_SECRET must be set to persist encryption key")
    fernet_key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    f = Fernet(fernet_key)
    _SESSION_KEY_PATH.write_bytes(f.encrypt(key))
    _SESSION_KEY_PATH.chmod(0o600)


def load_session_key() -> bytes | None:
    """Load encryption key from session file if it exists."""
    if not _SESSION_KEY_PATH.exists():
        return None
    from cryptography.fernet import Fernet, InvalidToken
    import hashlib, base64
    from app.config import get_settings
    secret = get_settings().web.session_secret
    if not secret:
        return None
    fernet_key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    try:
        f = Fernet(fernet_key)
        return f.decrypt(_SESSION_KEY_PATH.read_bytes())
    except (InvalidToken, Exception):
        _SESSION_KEY_PATH.unlink(missing_ok=True)
        return None


def clear_session_key():
    """Remove session key file (on lock)."""
    _SESSION_KEY_PATH.unlink(missing_ok=True)


def _apply_encryption_key(key: bytes):
    """Apply encryption key to all running storage layers (DB, files, vectors)."""
    db = _get("db")
    if db:
        db._enc_key = key
    fs = _get("file_storage")
    if fs:
        for backend in fs._backends.values():
            if hasattr(backend, "_encryption_key"):
                backend._encryption_key = key
    vs = _get("vector_store")
    if vs:
        vs._strip_text = True
    # Update vault encryption key
    vault = _get("note_vault")
    if vault:
        vault._enc_key = key
    # Also update via legacy note_agent.vault compat
    note_agent = _get("note_agent")
    if note_agent and hasattr(note_agent, "vault") and note_agent.vault:
        note_agent.vault._enc_key = key


# ── Encryption Settings ──────────────────────────────────────────────────

@router.post("/settings/encryption/setup")
@limiter.limit("3/minute")
async def encryption_setup(
    request: Request,
    master_password: str = Form(...),
    confirm_password: str = Form(...),
):
    """Set up master password for encryption (first time)."""
    from app.utils.crypto import (
        is_encryption_configured,
        setup_master_password,
        generate_recovery_key,
    )

    if is_encryption_configured():
        return RedirectResponse("/settings/security?error=already_configured", status_code=303)

    if master_password != confirm_password:
        return RedirectResponse("/settings/security?error=passwords_mismatch", status_code=303)

    if len(master_password) < 8:
        return RedirectResponse("/settings/security?error=password_too_short", status_code=303)

    # Load optional key file
    key_file_data = None
    import os
    kf_path = os.environ.get("KEY_FILE", "")
    if kf_path and Path(kf_path).exists():
        key_file_data = Path(kf_path).read_bytes()

    key = setup_master_password(master_password, "data/encryption.key", key_file_data)

    # Generate recovery key
    recovery = generate_recovery_key(key)
    recovery_file = Path("data/RECOVERY_KEY.txt")
    recovery_file.parent.mkdir(parents=True, exist_ok=True)
    recovery_file.write_text(
        "КЛЮЧ ВОССТАНОВЛЕНИЯ (RECOVERY KEY)\n"
        "====================================\n"
        f"{recovery}\n\n"
        "Сохраните этот ключ в менеджере паролей и УДАЛИТЕ этот файл.\n"
        "Без этого ключа расшифровать данные НЕВОЗМОЖНО.\n"
    )

    # Store key in app state and apply to ALL storage layers
    from app.main import _state
    _state["_encryption_key"] = key
    _apply_encryption_key(key)

    return RedirectResponse("/settings/security?saved=true", status_code=303)


@router.post("/settings/encryption/unlock")
@limiter.limit("5/minute")
async def encryption_unlock(
    request: Request,
    master_password: str = Form(...),
):
    """Unlock encryption with master password."""
    from app.utils.crypto import is_encryption_configured, unlock_with_password
    import os

    if not is_encryption_configured():
        return RedirectResponse("/settings/security?error=not_configured", status_code=303)

    key_file_data = None
    kf_path = os.environ.get("KEY_FILE", "")
    if kf_path and Path(kf_path).exists():
        key_file_data = Path(kf_path).read_bytes()

    try:
        key = unlock_with_password(master_password, "data/encryption.key", key_file_data)
    except ValueError as e:
        return RedirectResponse(f"/settings?error={e}", status_code=303)

    # Apply key to ALL storage layers
    from app.main import _state
    _state["_encryption_key"] = key
    _apply_encryption_key(key)

    # Persist session key so it survives uvicorn reload
    _save_session_key(key)

    return RedirectResponse("/settings/security?saved=true", status_code=303)


@router.post("/settings/encryption/lock")
async def encryption_lock(request: Request):
    """Lock encryption — clear key from memory and session."""
    from app.main import _state
    _state["_encryption_key"] = None
    db = _get("db")
    if db:
        db._enc_key = None
    fs = _get("file_storage")
    if fs:
        for backend in fs._backends.values():
            if hasattr(backend, "_encryption_key"):
                backend._encryption_key = None
    clear_session_key()
    return RedirectResponse("/settings/security?locked=true", status_code=303)


@router.get("/settings/encryption/recovery")
async def encryption_recovery(request: Request):
    """Download recovery key if file exists."""
    recovery_file = Path("data/RECOVERY_KEY.txt")
    if not recovery_file.exists():
        return RedirectResponse("/settings/security?error=no_recovery", status_code=303)
    from starlette.responses import Response
    return Response(
        content=recovery_file.read_text(),
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="RECOVERY_KEY.txt"'},
    )


# ── Storage Backend Settings ─────────────────────────────────────────────

@router.post("/settings/storage/save")
async def storage_save(request: Request):
    """Save storage backend configuration to config.yaml."""
    import yaml as yaml_lib
    form = await request.form()
    backend = form.get("backend", "local")

    config_path = Path("config.yaml")
    config = yaml_lib.safe_load(config_path.read_text()) if config_path.exists() else {}
    config.setdefault("storage", {})
    config["storage"]["backend"] = backend

    if backend == "s3":
        config["storage"]["s3"] = {
            "bucket": form.get("s3_bucket", ""),
            "region": form.get("s3_region", "us-east-1"),
            "prefix": form.get("s3_prefix", "fileagent"),
            "endpoint_url": form.get("s3_endpoint", ""),
            "access_key_id": form.get("s3_access_key", ""),
            "secret_access_key": form.get("s3_secret_key", ""),
        }
    elif backend == "gdrive":
        config["storage"]["gdrive"] = {
            "credentials_json": form.get("gdrive_credentials", ""),
            "folder_id": form.get("gdrive_folder_id", ""),
        }

    config_path.write_text(yaml_lib.dump(config, default_flow_style=False, allow_unicode=True))
    return RedirectResponse("/settings/storage?saved=true", status_code=303)


@router.post("/settings/storage/test")
async def storage_test(request: Request):
    """Test connection to selected storage backend."""
    form = await request.form()
    backend = form.get("backend", "local")

    if backend == "local":
        return HTMLResponse('<span class="text-ok-text text-xs">Local disk OK</span>')

    if backend == "s3":
        try:
            from app.storage.backends.s3 import S3Backend
            s3 = S3Backend(
                bucket=form.get("s3_bucket", ""),
                region=form.get("s3_region", "us-east-1"),
                access_key_id=form.get("s3_access_key", ""),
                secret_access_key=form.get("s3_secret_key", ""),
                endpoint_url=form.get("s3_endpoint", ""),
            )
            result = await s3.test_connection()
            if result["status"] == "ok":
                return HTMLResponse(f'<span class="text-ok-text text-xs">S3 OK: {result["bucket"]}</span>')
            return HTMLResponse(f'<span class="text-err-text text-xs">Error: {result["error"]}</span>')
        except Exception as e:
            return HTMLResponse(f'<span class="text-err-text text-xs">Error: {e}</span>')

    if backend == "gdrive":
        try:
            from app.storage.backends.gdrive import GDriveBackend
            gd = GDriveBackend(
                credentials_json=form.get("gdrive_credentials", ""),
                folder_id=form.get("gdrive_folder_id", ""),
            )
            result = await gd.test_connection()
            if result["status"] == "ok":
                return HTMLResponse(f'<span class="text-ok-text text-xs">GDrive OK: {result["folder"]}</span>')
            return HTMLResponse(f'<span class="text-err-text text-xs">Error: {result["error"]}</span>')
        except Exception as e:
            return HTMLResponse(f'<span class="text-err-text text-xs">Error: {e}</span>')

    return HTMLResponse('<span class="text-txt-3 text-xs">Unknown backend</span>')




@router.get("/folders", response_class=HTMLResponse)
async def folders_page(request: Request):
    db = _get("db")
    folders = await db.list_folders() if db else []
    return templates.TemplateResponse("folders.html", {
        "request": request, "page": "folders", "folders": folders,
    })


@router.post("/folders/create", response_class=HTMLResponse)
async def create_folder(request: Request, name: str = Form(...), description: str = Form("")):
    db = _get("db")
    if db:
        await db.create_folder(name, description)
    return RedirectResponse("/folders", status_code=303)


@router.post("/folders/{folder_id}/delete")
async def delete_folder(folder_id: int):
    db = _get("db")
    if db:
        await db.delete_folder(folder_id)
    return RedirectResponse("/folders", status_code=303)


@router.get("/folders/{folder_id}", response_class=HTMLResponse)
async def folder_detail(request: Request, folder_id: int):
    db = _get("db")
    folders = await db.list_folders() if db else []
    folder = next((f for f in folders if f["id"] == folder_id), None)
    files = await db.list_files_in_folder(folder_id) if db else []
    all_files = await db.list_files(limit=200) if db else []
    return templates.TemplateResponse("folder_detail.html", {
        "request": request, "page": "folders", "folder": folder,
        "files": files, "all_files": all_files,
    })


@router.post("/folders/{folder_id}/add-file")
async def add_file_to_folder(folder_id: int, file_id: str = Form(...)):
    db = _get("db")
    if db:
        await db.add_file_to_folder(file_id, folder_id)
    return RedirectResponse(f"/folders/{folder_id}", status_code=303)


@router.post("/folders/{folder_id}/remove-file/{file_id}")
async def remove_file_from_folder(folder_id: int, file_id: str):
    db = _get("db")
    if db:
        await db.remove_file_from_folder(file_id, folder_id)
    return RedirectResponse(f"/folders/{folder_id}", status_code=303)


@router.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request):
    db = _get("db")
    insights = await db.get_all_insights() if db else []
    return templates.TemplateResponse("insights.html", {
        "request": request, "page": "insights", "insights": insights,
    })


@router.post("/insights/refresh")
async def insights_refresh(request: Request):
    insights_engine = _get("insights_engine")
    if insights_engine:
        await insights_engine.refresh_all()
    return RedirectResponse("/insights", status_code=303)


@router.get("/reminders", response_class=HTMLResponse)
async def reminders_page(request: Request):
    db = _get("db")
    reminders = await db.list_reminders() if db else []
    for r in reminders:
        try:
            meta = json.loads(r.get("metadata_json", "{}") or "{}")
            r["document_type"] = meta.get("document_type", "")
        except Exception:
            r["document_type"] = ""
    return templates.TemplateResponse("reminders.html", {
        "request": request, "page": "reminders", "reminders": reminders,
    })


# ── Notes ──────────────────────────────────────────────────────────────────

async def _build_dashboard_vm(today_data: dict, analytics, yesterday_data: dict,
                              sparklines: dict, correlations: list,
                              notes_count: int, days_active: int,
                              due_tasks: int, review_count: int, reminders_count: int,
                              top_actions: list) -> dict:
    """Build the complete dashboard view-model."""
    from datetime import datetime

    metrics = today_data.get("metrics", {})
    y_metrics = yesterday_data.get("metrics", {})

    # ── Maturity ──
    if notes_count <= 2:
        maturity = "empty"
    elif notes_count <= 20 or days_active < 7:
        maturity = "early"
    else:
        maturity = "mature"

    # ── Helper: get metric value ──
    def _mv(m, key="avg"):
        v = m.get(key, {})
        if isinstance(v, dict):
            return v.get(key) or v.get("total")
        return None

    mood_val = _mv(metrics, "mood_score")
    sleep_val = _mv(metrics, "sleep_hours")
    cal_val = None
    cal_data = metrics.get("calories", {})
    if isinstance(cal_data, dict):
        cal_val = cal_data.get("total") or cal_data.get("avg")
    weight_val = _mv(metrics, "weight_kg")

    y_mood = _mv(y_metrics, "mood_score")
    y_sleep = _mv(y_metrics, "sleep_hours")
    y_cal_data = y_metrics.get("calories", {})
    y_cal = y_cal_data.get("total") or y_cal_data.get("avg") if isinstance(y_cal_data, dict) else None
    y_weight = _mv(y_metrics, "weight_kg")

    # ── Hero scoring ──
    score = 0
    if sleep_val is not None:
        score += 2 if sleep_val >= 7 else (-2 if sleep_val < 6 else 0)
    if mood_val is not None:
        score += 2 if mood_val >= 7 else (-2 if mood_val <= 4 else 0)
    if due_tasks > 3:
        score -= 1
    if reminders_count > 0:
        score -= 0.5

    if score >= 3:
        state_label, orb_tone = "Хороший ритм", "green"
    elif score >= 0:
        state_label, orb_tone = "Стабильный день", "blue"
    elif score >= -2:
        state_label, orb_tone = "Нужен фокус", "orange"
    else:
        state_label, orb_tone = "Режим восстановления", "red"

    warning = None
    recommendation = None
    if sleep_val is not None and sleep_val < 6:
        warning = f"Сон {sleep_val:.1f}ч — ниже нормы"
        recommendation = "Сегодня не ставь тяжёлую тренировку"
    elif due_tasks > 0:
        recommendation = f"{due_tasks} задач требуют внимания"

    # Supporting line
    parts = []
    if today_data.get("notes_count"):
        parts.append(f"{today_data['notes_count']} заметок")
    if cal_val:
        parts.append(f"{int(cal_val)} kcal")
    if sleep_val:
        parts.append(f"{sleep_val:.1f}ч сна")
    supporting = " · ".join(parts) if parts else None

    hero = {
        "state_label": state_label, "orb_tone": orb_tone,
        "warning": warning, "recommendation": recommendation,
        "supporting_line": supporting,
    }

    # ── KPI cards ──
    def _kpi(key, label, unit, current, yesterday, target_min, target_max,
             higher_is_better, points):
        if current is None:
            delta = None
            trend_dir = "stable"
            trend_good = True
            state = "Stable"
        else:
            delta = (current - yesterday) if yesterday is not None else None
            if delta is not None:
                if abs(delta) < 0.1:
                    trend_dir = "stable"
                elif delta > 0:
                    trend_dir = "up"
                else:
                    trend_dir = "down"
                trend_good = (delta > 0) == higher_is_better if delta != 0 else True
            else:
                trend_dir = "stable"
                trend_good = True

            if key == "weight":
                # Weight: trend-oriented stability
                state = "Stable"
                if points and len(points) >= 3:
                    recent = [p for p in points[-7:] if p is not None]
                    if len(recent) >= 2 and abs(recent[-1] - recent[0]) > 1.5:
                        state = "Needs attention"
            elif target_min is not None and target_max is not None:
                if target_min <= (current or 0) <= target_max:
                    state = "On track"
                elif (current or 0) < target_min:
                    state = "Below baseline" if (current or 0) >= target_min * 0.7 else "Needs attention"
                else:
                    state = "On track"  # above target is fine for most metrics
            else:
                state = "Stable"

        return {
            "key": key, "label": label, "unit": unit,
            "current": current,
            "delta_vs_yesterday": delta,
            "delta_label": "vs вчера",
            "trend_direction": trend_dir,
            "trend_good": trend_good,
            "state_label": state,
            "target_min": target_min, "target_max": target_max,
            "points": points or [],
        }

    sp_mood = [p.get("avg") for p in sparklines.get("mood", [])]
    sp_sleep = [p.get("avg") for p in sparklines.get("sleep", [])]
    sp_cal = [p.get("total", p.get("avg", 0)) for p in sparklines.get("cal", [])]
    sp_weight = [p.get("avg") for p in sparklines.get("weight", [])]

    kpis = [
        _kpi("mood", "Настроение", "/10", mood_val, y_mood, 6, 8, True, sp_mood),
        _kpi("sleep", "Сон", "ч", sleep_val, y_sleep, 7, 9, True, sp_sleep),
        _kpi("calories", "Калории", "kcal", cal_val, y_cal, None, None, True, sp_cal),
        _kpi("weight", "Вес", "кг", weight_val, y_weight, None, None, False, sp_weight),
    ]

    # ── Insights (max 3) ──
    insights = []
    if sleep_val is not None and sleep_val < 6:
        insights.append({
            "type": "warning", "headline": "Недосып",
            "why": f"Сон {sleep_val:.1f}ч — ниже 7ч нормы",
            "action": "Ложись раньше, не ставь тяжёлую тренировку",
        })
    if mood_val is not None and y_mood is not None and y_mood - mood_val >= 3:
        insights.append({
            "type": "warning", "headline": "Резкое падение настроения",
            "why": f"Настроение упало с {y_mood:.0f} до {mood_val:.0f}",
            "action": "Сделай паузу, прогуляйся",
        })
    if due_tasks > 2:
        insights.append({
            "type": "action", "headline": f"{due_tasks} задач на сегодня",
            "why": "Есть просроченные или срочные задачи",
            "action": "Начни с самой важной",
        })
    # Check for streak wins from habits
    try:
        from app.notes.habits import HabitTracker
        tracker = HabitTracker(_get("db"))
        today_str = datetime.now().strftime("%Y-%m-%d")
        habit_statuses = await tracker.check_habits_for_date(today_str)
        for h in habit_statuses:
            if h.get("streak", 0) >= 5 and len(insights) < 3:
                insights.append({
                    "type": "win", "headline": f"{h['name']} — {h['streak']} дней подряд",
                    "why": "Стабильная привычка формируется",
                    "action": "Продолжай в том же духе",
                })
                break
    except Exception:
        pass

    insights = insights[:3]

    # ── Correlations (filtered) ──
    filtered_corrs = []
    if maturity == "mature":
        for c in correlations:
            if c.get("correlation") is not None and abs(c["correlation"]) >= 0.25:
                if c.get("data_points", 999) >= 14:
                    filtered_corrs.append(c)
        filtered_corrs = filtered_corrs[:3]

    actions = {
        "due_tasks": due_tasks, "review_count": review_count,
        "reminders_count": reminders_count,
        "top_items": top_actions[:3],
    }

    return {
        "maturity": maturity,
        "hero": hero,
        "kpis": kpis,
        "insights": insights,
        "actions": actions,
        "correlations": filtered_corrs,
        "days_active": days_active,
    }


@router.get("/notes", response_class=HTMLResponse)
async def notes_page(request: Request, category: str = ""):
    from datetime import datetime, timedelta
    db = _get("db")
    if not db:
        return templates.TemplateResponse("notes_dashboard.html", {
            "request": request, "page": "notes", "notes": [],
            "today": "", "dashboard": {"maturity": "empty", "hero": {"state_label": "Добро пожаловать", "orb_tone": "blue", "warning": None, "recommendation": "Отправьте первую заметку в Telegram", "supporting_line": None}, "kpis": [], "insights": [], "actions": {"due_tasks": 0, "review_count": 0, "reminders_count": 0, "top_items": []}, "correlations": [], "days_active": 0},
            "categories": [], "current_category": "",
        })

    notes = await db.list_notes(limit=100, category=category)
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    from app.notes.analytics import NoteAnalytics
    analytics = NoteAnalytics(db)
    today_data = await analytics.get_daily_summary_data(today)
    yesterday_data = await analytics.get_daily_summary_data(yesterday)
    correlations = await analytics.get_all_correlations(days=60)
    cat_dist = await db.get_category_distribution(days=30)
    categories = list(cat_dist.keys())

    # Sparklines (7-day trends)
    sparklines = {
        "mood": await analytics.get_mood_trend(7),
        "sleep": await analytics.get_sleep_trend(7) if hasattr(analytics, 'get_sleep_trend') else await analytics._get_trend("sleep_hours", 7),
        "cal": await analytics.get_calorie_trend(7),
        "weight": await analytics.get_weight_trend(7),
    }

    # Counts for actions
    total_notes = len(await db.list_notes(limit=9999))
    days_active_cursor = await db.db.execute(
        "SELECT COUNT(DISTINCT date(created_at)) FROM notes"
    )
    days_active = (await days_active_cursor.fetchone())[0] or 0

    due_tasks = 0
    review_count = 0
    reminders_count = 0
    top_actions = []
    try:
        cursor = await db.db.execute(
            "SELECT COUNT(*) FROM note_tasks WHERE status='open' AND due_date <= ?", (today,)
        )
        due_tasks = (await cursor.fetchone())[0] or 0
        cursor = await db.db.execute(
            "SELECT COUNT(*) FROM notes WHERE status='needs_review'"
        )
        review_count = (await cursor.fetchone())[0] or 0
        cursor = await db.db.execute(
            "SELECT COUNT(*) FROM note_reminders WHERE status='pending'"
        )
        reminders_count = (await cursor.fetchone())[0] or 0
        cursor = await db.db.execute(
            "SELECT description FROM note_tasks WHERE status='open' AND due_date <= ? ORDER BY due_date LIMIT 3",
            (today,),
        )
        top_actions = [r[0] for r in await cursor.fetchall()]
    except Exception:
        pass

    dashboard = await _build_dashboard_vm(
        today_data, analytics, yesterday_data, sparklines, correlations,
        total_notes, days_active, due_tasks, review_count, reminders_count, top_actions,
    )

    return templates.TemplateResponse("notes_dashboard.html", {
        "request": request, "page": "notes", "notes": notes,
        "today": today, "dashboard": dashboard,
        "categories": categories, "current_category": category,
    })


@router.post("/notes/create")
async def notes_create(request: Request, content: str = Form(...), title: str = Form("")):
    capture = _get("note_capture")
    if capture and content.strip():
        await capture.capture(content.strip(), source="web", title=title.strip())
    elif content.strip():
        db = _get("db")
        if db:
            await db.save_note(content=content.strip(), title=title.strip(), source="web")
    return RedirectResponse("/notes", status_code=303)


# ── Notes: static routes MUST come before /notes/{note_id} ──────────

@router.get("/notes/graph", response_class=HTMLResponse)
async def notes_graph(request: Request, center: int = 0):
    """Knowledge graph visualization."""
    return templates.TemplateResponse("note_graph.html", {
        "request": request, "page": "notes", "center_id": center,
    })


@router.get("/api/notes/graph")
async def notes_graph_api(request: Request, center: int = 0, limit: int = 100):
    """JSON API for graph data."""
    db = _get("db")
    if not db:
        return JSONResponse({"nodes": [], "edges": []})
    limit = min(max(limit, 10), 500)
    data = await db.get_graph_data(center, limit)
    return JSONResponse(data)


@router.get("/notes/habits", response_class=HTMLResponse)
async def notes_habits(request: Request):
    """Habit tracking page — define habits, view streaks."""
    db = _get("db")
    if not db:
        return HTMLResponse("Database not available", status_code=500)
    from app.notes.habits import HabitTracker
    tracker = HabitTracker(db)
    today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
    habit_statuses = await tracker.check_habits_for_date(today)
    return templates.TemplateResponse("note_habits.html", {
        "request": request, "page": "notes",
        "habits": habit_statuses, "today": today,
    })


@router.post("/notes/habits/create")
async def notes_habits_create(
    request: Request, name: str = Form(""), metric_key: str = Form(""),
    target_value: float = Form(1), frequency: str = Form("daily"),
):
    db = _get("db")
    if db and name:
        from app.notes.habits import HabitTracker
        await HabitTracker(db).create_habit(name, frequency, target_value, metric_key)
    return RedirectResponse("/notes/habits", status_code=303)


@router.post("/notes/habits/{habit_id}/delete")
async def notes_habits_delete(request: Request, habit_id: int):
    db = _get("db")
    if db:
        from app.notes.habits import HabitTracker
        await HabitTracker(db).delete_habit(habit_id)
    return RedirectResponse("/notes/habits", status_code=303)


@router.post("/notes/habits/{habit_id}/toggle")
async def notes_habits_toggle(request: Request, habit_id: int):
    db = _get("db")
    if db:
        from app.notes.habits import HabitTracker
        today = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        await HabitTracker(db).toggle_habit_entry(habit_id, today)
    return RedirectResponse("/notes/habits", status_code=303)


@router.get("/notes/entities", response_class=HTMLResponse)
async def notes_entities(request: Request):
    """Entity management page."""
    db = _get("db")
    clusters = await db.get_all_entity_clusters() if db else []
    duplicates = await db.find_duplicate_entities() if db else []
    return templates.TemplateResponse("note_entities.html", {
        "request": request, "page": "notes",
        "clusters": clusters, "duplicates": duplicates,
    })


@router.post("/notes/entities/merge")
async def notes_entities_merge(
    request: Request, entity_type: str = Form(""),
    canonical: str = Form(""), aliases: str = Form(""),
):
    db = _get("db")
    if db and entity_type and canonical and aliases:
        alias_list = [a.strip() for a in aliases.split(",") if a.strip()]
        await db.merge_entities(entity_type, canonical, alias_list)
    return RedirectResponse("/notes/entities", status_code=303)


@router.get("/notes/review", response_class=HTMLResponse)
async def notes_review(request: Request):
    db = _get("db")
    needs_review = await db.get_notes_by_status("needs_review") if db else []
    failed = await db.get_notes_by_status("failed") if db else []
    return templates.TemplateResponse("notes_review.html", {
        "request": request, "page": "notes",
        "needs_review": needs_review, "failed": failed,
    })


@router.get("/notes/reminders", response_class=HTMLResponse)
async def notes_reminders(request: Request):
    db = _get("db")
    reminders = await db.list_note_reminders() if db else []
    return templates.TemplateResponse("note_reminders.html", {
        "request": request, "page": "notes", "reminders": reminders,
    })


@router.get("/notes/export")
async def notes_export(request: Request, fmt: str = "csv", kind: str = "notes",
                       from_date: str = "", to_date: str = ""):
    """Export notes data as CSV or JSON."""
    fmt = request.query_params.get("format", fmt)
    kind = request.query_params.get("type", kind)
    from starlette.responses import Response
    db = _get("db")
    if not db:
        return HTMLResponse("Database not available", status_code=500)
    from app.notes.export import ExportService
    svc = ExportService(db)
    now = __import__("datetime").datetime.now().strftime("%Y%m%d")
    if fmt == "json":
        data = await svc.export_all_json(from_date, to_date)
        return Response(
            content=data, media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="notes_export_{now}.json"'},
        )
    if kind == "food":
        data = await svc.export_food_csv(from_date, to_date)
        filename = f"food_{now}.csv"
    elif kind == "facts":
        data = await svc.export_facts_csv(from_date=from_date, to_date=to_date)
        filename = f"facts_{now}.csv"
    else:
        data = await svc.export_notes_csv(from_date, to_date)
        filename = f"notes_{now}.csv"
    return Response(
        content=data, media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Notes: dynamic {note_id} routes ────────────────────────────────

@router.post("/notes/{note_id}/delete")
async def notes_delete(request: Request, note_id: int):
    db = _get("db")
    if db:
        await db.delete_note(note_id)
    return RedirectResponse("/notes", status_code=303)


@router.post("/notes/{note_id}/reprocess")
async def notes_reprocess(request: Request, note_id: int):
    """Enqueue note for re-enrichment. Clears manual override so AI can update."""
    db = _get("db")
    if db:
        await db.db.execute("UPDATE notes SET metadata_manual=0 WHERE id=?", (note_id,))
        await db.db.commit()
    capture = _get("note_capture")
    if capture:
        await capture.enqueue_enrichment(note_id)
    return RedirectResponse("/notes", status_code=303)


@router.post("/notes/{note_id}/approve")
async def notes_approve(request: Request, note_id: int):
    """Approve a needs_review note → enriched."""
    db = _get("db")
    if db:
        await db.set_note_status(note_id, "enriched")
    return RedirectResponse(f"/notes/{note_id}", status_code=303)


@router.post("/notes/{note_id}/edit")
async def notes_edit(request: Request, note_id: int):
    """Save manual metadata edits (title, category, subcategory, tags)."""
    db = _get("db")
    if not db:
        return RedirectResponse(f"/notes/{note_id}", status_code=303)

    form = await request.form()
    user_title = form.get("user_title", "").strip()
    category = form.get("category", "").strip()
    subcategory = form.get("subcategory", "").strip()
    tags_raw = form.get("tags", "").strip()

    import json as _json
    tags_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
    tags_json = _json.dumps(tags_list, ensure_ascii=False)

    await db.update_note_metadata(
        note_id=note_id,
        user_title=user_title,
        category=category,
        subcategory=subcategory,
        tags=tags_json,
    )

    # Auto-approve if currently in review
    note = await db.get_note(note_id)
    if note and note.get("status") == "needs_review":
        await db.set_note_status(note_id, "enriched")

    return RedirectResponse(f"/notes/{note_id}", status_code=303)


@router.post("/notes/{note_id}/archive")
async def notes_archive(request: Request, note_id: int):
    db = _get("db")
    if db:
        await db.set_note_status(note_id, "archived")
    return RedirectResponse("/notes", status_code=303)


@router.post("/notes/bulk/archive")
async def notes_bulk_archive(request: Request, note_ids: str = Form("")):
    db = _get("db")
    if db and note_ids:
        ids = [int(x) for x in note_ids.split(",") if x.strip().isdigit()]
        count = await db.bulk_archive_notes(ids)
    return RedirectResponse("/notes", status_code=303)


@router.post("/notes/bulk/delete")
async def notes_bulk_delete(request: Request, note_ids: str = Form("")):
    db = _get("db")
    if db and note_ids:
        ids = [int(x) for x in note_ids.split(",") if x.strip().isdigit()]
        count = await db.bulk_delete_notes(ids)
    return RedirectResponse("/notes", status_code=303)


@router.post("/notes/bulk/category")
async def notes_bulk_category(request: Request, note_ids: str = Form(""), category: str = Form("")):
    db = _get("db")
    if db and note_ids and category:
        ids = [int(x) for x in note_ids.split(",") if x.strip().isdigit()]
        count = await db.bulk_set_category(ids, category)
    return RedirectResponse("/notes", status_code=303)


@router.post("/notes/{note_id}/pin")
async def notes_pin(request: Request, note_id: int):
    db = _get("db")
    if db:
        await db.toggle_note_pin(note_id)
    referer = request.headers.get("referer", "/notes")
    return RedirectResponse(referer, status_code=303)


# ── Note Detail + Chart Partials ─────────────────────────────────────────

@router.get("/notes/{note_id}", response_class=HTMLResponse)
async def note_detail(request: Request, note_id: int):
    """Note detail page — raw text, enrichment, entities, facts, tasks, relations."""
    db = _get("db")
    if not db:
        return RedirectResponse("/notes", status_code=303)

    note = await db.get_note(note_id)
    if not note:
        return RedirectResponse("/notes", status_code=303)

    enrichment = await db.get_latest_enrichment(note_id)
    entities = await db.get_entities_by_note(note_id)
    facts = await db.get_facts_by_note(note_id)
    tasks = await db.get_tasks_by_note(note_id)
    relations = await db.get_note_relations_v2(note_id)
    enrichment_history = await db.get_enrichment_history(note_id)

    from app.notes.categorizer import CATEGORIES
    return templates.TemplateResponse("note_detail.html", {
        "request": request, "page": "notes",
        "note": note, "enrichment": enrichment,
        "entities": entities, "facts": facts,
        "tasks": tasks, "relations": relations,
        "categories": CATEGORIES,
        "enrichment_history": enrichment_history,
    })


@router.post("/notes/tasks/{task_id}/toggle")
async def toggle_task(request: Request, task_id: int):
    """Toggle task status between open and done."""
    db = _get("db")
    if db:
        cursor = await db.db.execute("SELECT status, note_id FROM note_tasks WHERE id=?", (task_id,))
        row = await cursor.fetchone()
        if row:
            new_status = "done" if row[0] == "open" else "open"
            await db.update_task_status(task_id, new_status)
            return RedirectResponse(f"/notes/{row[1]}", status_code=303)
    return RedirectResponse("/notes", status_code=303)


def _chart_context(data: list, chart_color: str, chart_color_light: str,
                   chart_label: str, min_val: float = 0,
                   target_min: float | None = None, target_max: float | None = None,
                   value_key: str = "avg") -> dict:
    """Build unified chart partial context."""
    if not data:
        return {"data": [], "empty_mode": "empty", "chart_color": chart_color}

    vals = [d.get(value_key, 0) or 0 for d in data]
    actual_min = min(vals) if vals else 0
    actual_max = max(vals) if vals else 1
    if min_val == 0 and actual_min > 0 and chart_label == "кг":
        min_val = max(0, actual_min - 2)  # Weight: don't start at 0
    max_val = actual_max if actual_max > min_val else min_val + 1

    return {
        "data": data, "max_val": max_val, "min_val": min_val,
        "chart_color": chart_color, "chart_color_light": chart_color_light,
        "chart_label": chart_label, "value_key": value_key,
        "target_min": target_min, "target_max": target_max,
        "empty_mode": "single" if len(data) == 1 else "none",
    }


@router.get("/partials/note-mood-chart", response_class=HTMLResponse)
async def note_mood_chart(request: Request, days: int = 30):
    db = _get("db")
    days = min(max(days, 7), 365)
    from app.notes.analytics import NoteAnalytics
    data = await NoteAnalytics(db).get_mood_trend(days) if db else []
    ctx = _chart_context(data, "#a78bfa", "#7c3aed", "/10", min_val=0,
                         target_min=6, target_max=8)
    ctx["max_val"] = 10
    return templates.TemplateResponse("partials/note_charts.html", {"request": request, **ctx})


@router.get("/partials/note-calories-chart", response_class=HTMLResponse)
async def note_calories_chart(request: Request, days: int = 30):
    db = _get("db")
    days = min(max(days, 7), 365)
    from app.notes.analytics import NoteAnalytics
    data = await NoteAnalytics(db).get_calorie_trend(days) if db else []
    for d in data:
        d["avg"] = d.get("total", d.get("avg", 0))
    ctx = _chart_context(data, "#fb923c", "#f59e0b", "kcal")
    return templates.TemplateResponse("partials/note_charts.html", {"request": request, **ctx})


@router.get("/partials/note-weight-chart", response_class=HTMLResponse)
async def note_weight_chart(request: Request, days: int = 90):
    db = _get("db")
    days = min(max(days, 7), 365)
    from app.notes.analytics import NoteAnalytics
    data = await NoteAnalytics(db).get_weight_trend(days) if db else []
    ctx = _chart_context(data, "#4ade80", "#22c55e", "кг")
    return templates.TemplateResponse("partials/note_charts.html", {"request": request, **ctx})


@router.get("/partials/note-category-dist", response_class=HTMLResponse)
async def note_category_dist(request: Request, days: int = 30):
    db = _get("db")
    days = min(max(days, 7), 365)
    distribution = await db.get_category_distribution(days) if db else {}
    total = sum(distribution.values()) if distribution else 0
    return templates.TemplateResponse("partials/note_category_dist.html", {
        "request": request, "distribution": distribution, "total": total,
    })


@router.get("/partials/note-heatmap", response_class=HTMLResponse)
async def note_heatmap(request: Request, days: int = 90):
    """Calendar heatmap of note activity."""
    from datetime import datetime, timedelta
    db = _get("db")
    days = min(max(days, 28), 365)
    counts = await db.get_notes_count_by_date(days) if db else {}
    max_count = max(counts.values()) if counts else 1

    # Build cell grid: each day → {date, count, weekday (0=Mon), week}
    cells = []
    today = datetime.now().date()
    start = today - timedelta(days=days - 1)
    # Align start to Monday
    start = start - timedelta(days=start.weekday())
    weeks = 0
    current = start
    while current <= today:
        date_str = current.strftime("%Y-%m-%d")
        cells.append({
            "date": date_str,
            "count": counts.get(date_str, 0),
            "weekday": current.weekday(),
            "week": (current - start).days // 7,
        })
        if current.weekday() == 6:
            weeks = (current - start).days // 7
        current += timedelta(days=1)
    weeks += 1

    return templates.TemplateResponse("partials/note_heatmap.html", {
        "request": request, "cells": cells, "max_count": max_count, "weeks": weeks,
    })


@router.get("/partials/note-search", response_class=HTMLResponse)
async def note_search_partial(
    request: Request, q: str = "", category: str = "",
    status: str = "", source: str = "", mode: str = "keyword",
):
    """Search notes: keyword (LIKE on unencrypted fields), semantic (Qdrant), or hybrid."""
    db = _get("db")
    notes: list[dict] = []
    semantic_ids: dict[int, float] = {}

    if not db:
        return templates.TemplateResponse("partials/note_list.html", {
            "request": request, "notes": [], "semantic_ids": {},
        })

    if not q.strip():
        # No query — list with filters only
        notes = await db.list_notes(limit=50, category=category)
        if status:
            notes = [n for n in notes if n.get("status") == status]
        if source:
            notes = [n for n in notes if n.get("source") == source]
    else:
        # Keyword search
        notes = await db.search_notes_keyword(
            q.strip(), category=category, status=status, source=source,
        )

        # Semantic search (hybrid or semantic mode)
        if mode in ("semantic", "hybrid"):
            vector_store = _get("vector_store")
            if vector_store:
                try:
                    sem_results = await vector_store.search_notes(
                        q.strip(), category=category,
                    )
                    semantic_ids = {r["note_id"]: r["score"] for r in sem_results}

                    # Append semantic results not already in keyword results
                    keyword_ids = {n["id"] for n in notes}
                    for sem in sem_results:
                        if sem["note_id"] not in keyword_ids:
                            note = await db.get_note(sem["note_id"])
                            if note and note.get("status") != "archived":
                                notes.append(note)
                except Exception:
                    pass  # Qdrant unavailable — keyword only

    return templates.TemplateResponse("partials/note_list.html", {
        "request": request, "notes": notes,
        "semantic_ids": semantic_ids,
    })


@router.get("/notes/reminders", response_class=HTMLResponse)
async def note_reminders_page(request: Request):
    """Note reminders list page."""
    db = _get("db")
    reminders = await db.list_note_reminders(include_done=False) if db else []
    return templates.TemplateResponse("note_reminders.html", {
        "request": request, "page": "notes", "reminders": reminders,
    })


@router.post("/notes/reminders/{reminder_id}/done")
async def note_reminder_done(request: Request, reminder_id: int):
    db = _get("db")
    if db:
        await db.complete_note_reminder(reminder_id)
    return RedirectResponse("/notes/reminders", status_code=303)


@router.post("/notes/reminders/{reminder_id}/cancel")
async def note_reminder_cancel(request: Request, reminder_id: int):
    db = _get("db")
    if db:
        await db.cancel_note_reminder(reminder_id)
    return RedirectResponse("/notes/reminders", status_code=303)


@router.get("/partials/food-today", response_class=HTMLResponse)
async def food_today_partial(request: Request):
    """Today's nutrition breakdown from food_entries."""
    db = _get("db")
    if not db:
        return HTMLResponse("")
    from datetime import datetime as dt
    date = dt.now().strftime("%Y-%m-%d")
    entries = await db.get_food_entries_by_date(date)
    nutrition = await db.get_daily_nutrition(date)
    # Group entries by meal_type
    meals: dict[str, list] = {}
    for e in entries:
        mt = e.get("meal_type", "unknown")
        meals.setdefault(mt, []).append(e)
    return templates.TemplateResponse("partials/food_today.html", {
        "request": request, "entries": entries, "nutrition": nutrition,
        "meals": meals, "date": date,
    })


@router.get("/duplicates", response_class=HTMLResponse)
async def duplicates_page(request: Request):
    return templates.TemplateResponse("duplicates.html", {
        "request": request, "page": "duplicates", "groups": None, "scanned": False,
    })


@router.post("/duplicates/scan", response_class=HTMLResponse)
async def duplicates_scan(request: Request):
    """Scan all files for semantic duplicates using vector similarity."""
    import logging
    log = logging.getLogger(__name__)

    db = _get("db")
    vs = _get("vector_store")
    files = await db.list_files(limit=1000) if db else []

    # Build groups using union-find approach
    file_map = {f["id"]: f for f in files}
    parent = {f["id"]: f["id"] for f in files}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    scores = {}  # (id1, id2) → score

    for f in files:
        vec = vs.get_file_vector(f["id"]) if vs else None
        if not vec:
            continue
        similar = vs.find_similar(vector=vec, exclude_file_id=f["id"], threshold=0.94, top_k=5)
        for s in similar:
            if s.file_id in file_map:
                union(f["id"], s.file_id)
                key = tuple(sorted([f["id"], s.file_id]))
                scores[key] = max(scores.get(key, 0), s.score)

    # Collect groups
    groups_map = {}
    for fid in file_map:
        root = find(fid)
        groups_map.setdefault(root, []).append(fid)

    # Only keep groups with >1 file
    groups = []
    for root, members in groups_map.items():
        if len(members) < 2:
            continue
        group_files = sorted(
            [file_map[m] for m in members],
            key=lambda x: x.get("created_at", ""),
            reverse=True,
        )
        # Avg similarity score
        group_scores = [v for k, v in scores.items() if k[0] in members or k[1] in members]
        avg_score = sum(group_scores) / len(group_scores) if group_scores else 0
        groups.append({"files": group_files, "score": avg_score})

    groups.sort(key=lambda g: g["score"], reverse=True)
    log.info(f"Duplicate scan: {len(groups)} group(s) found")

    return templates.TemplateResponse("duplicates.html", {
        "request": request, "page": "duplicates", "groups": groups, "scanned": True,
    })


@router.post("/duplicates/resolve")
async def duplicates_resolve(request: Request):
    """Keep one file, delete the rest (cascading)."""
    import logging
    log = logging.getLogger(__name__)

    form = await request.form()
    keep_id = form.get("keep_id", "")
    delete_ids = form.getlist("delete_ids")

    if not keep_id or not delete_ids:
        return RedirectResponse("/duplicates", status_code=303)

    db = _get("db")
    vs = _get("vector_store")
    fs = _get("file_storage")

    for fid in delete_ids:
        if fid == keep_id:
            continue
        file = await db.get_file(fid)
        # Delete vectors
        if vs:
            try:
                await vs.delete_document(fid)
            except Exception as e:
                log.warning(f"Failed to delete vectors for {fid}: {e}")
        # Delete from disk
        fs = _get("file_storage")
        if file and file.get("stored_path") and fs:
            try:
                await fs.delete(file["stored_path"])
            except Exception as e:
                log.warning(f"Failed to delete file from disk: {e}")
        # Delete from DB
        await db.delete_file(fid)
        log.info(f"Duplicate resolved: deleted {fid}")

    return RedirectResponse("/duplicates", status_code=303)


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request, status: str | None = None, limit: int = 50):
    db = _get("db")
    logs = await db.get_recent_logs(limit=limit, status=status) if db else []
    return templates.TemplateResponse("logs.html", {
        "request": request, "page": "logs", "logs": logs, "status_filter": status,
    })


@router.get("/partials/stats", response_class=HTMLResponse)
async def partial_stats(request: Request):
    from datetime import date
    db = _get("db")
    vs = _get("vector_store")
    stats = await db.get_stats() if db else {}
    qdrant_health = await vs.health_check() if vs else {}
    today = date.today().isoformat()
    from app.config import get_settings as _gs
    _search_cfg = _gs().llm.models.get("search")
    is_oauth = bool(_search_cfg and _search_cfg.api_base)
    reminders_all = await db.list_reminders(include_sent=False) if db else []
    return templates.TemplateResponse("partials/stats_cards.html", {
        "request": request, "stats": stats,
        "qdrant_points": qdrant_health.get("points_count", 0),
        "total_queries": await db.get_total_queries() if db else 0,
        "today_queries": await db.get_total_queries(since=today) if db else 0,
        "processed": await db.get_processed_count() if db else 0,
        "errors": await db.get_error_count() if db else 0,
        "llm_all": await db.get_llm_stats() if db else {},
        "llm_today": await db.get_llm_stats(since=today) if db else {},
        "is_oauth": is_oauth,
        "pending_count": len(reminders_all),
    })


@router.get("/partials/query-history", response_class=HTMLResponse)
async def partial_query_history(request: Request):
    db = _get("db")
    history = await db.get_query_history(limit=15) if db else []
    return templates.TemplateResponse("partials/query_history.html", {
        "request": request, "query_history": history,
    })


@router.get("/partials/activity", response_class=HTMLResponse)
async def partial_activity(request: Request):
    db = _get("db")
    query_history = await db.get_query_history(limit=12) if db else []
    recent = await db.list_files(limit=8) if db else []
    activity = []
    for q in query_history:
        activity.append({"type": "search", "text": q.get("text", "?"), "source": q.get("source", "web"), "time": q.get("created_at", "")})
    for f in recent:
        activity.append({"type": "upload", "text": f["original_name"], "category": f.get("category", ""), "time": f.get("created_at", "")})
    activity.sort(key=lambda x: x.get("time", ""), reverse=True)
    return templates.TemplateResponse("partials/activity.html", {"request": request, "activity": activity[:12]})


@router.get("/partials/pipeline-health", response_class=HTMLResponse)
async def partial_pipeline_health(request: Request):
    db = _get("db")
    health = await db.get_pipeline_health(limit=5) if db else []
    return templates.TemplateResponse("partials/pipeline_health.html", {
        "request": request, "pipeline_health": health,
    })


@router.get("/partials/recent-files", response_class=HTMLResponse)
async def partial_recent(request: Request):
    db = _get("db")
    recent = await db.list_files(limit=10) if db else []
    return templates.TemplateResponse("partials/recent_files.html", {"request": request, "recent_files": recent})


@router.get("/partials/llm-stats", response_class=HTMLResponse)
async def partial_llm_stats(request: Request):
    from datetime import date
    db = _get("db")
    llm_session = _get("llm_router").get_stats() if _get("llm_router") else {}
    llm_all = await db.get_llm_stats() if db else {}
    llm_today = await db.get_llm_stats(since=date.today().isoformat()) if db else {}
    from app.config import get_settings as _gs
    _search_cfg = _gs().llm.models.get("search")
    is_oauth = bool(_search_cfg and _search_cfg.api_base)
    return templates.TemplateResponse("partials/llm_stats.html", {
        "request": request, "llm_session": llm_session, "llm_all": llm_all, "llm_today": llm_today,
        "is_oauth": is_oauth,
    })
