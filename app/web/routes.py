"""Web routes — dashboard, files, search, skills, settings, logs."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
router = APIRouter()

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


def _get(key: str):
    from app.main import get_state
    return get_state(key)


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
    classifier = _get("classifier")
    skill_engine = _get("skill_engine")
    if not db or not classifier:
        return RedirectResponse("/files", status_code=303)

    cursor = await db.db.execute(
        "SELECT id, original_name, extracted_text, mime_type FROM files "
        "WHERE category='uncategorized' OR category='' OR category IS NULL"
    )
    files = [dict(r) for r in await cursor.fetchall()]
    count = 0
    for f in files:
        text = f.get("extracted_text", "") or ""
        if not text.strip():
            continue
        try:
            result = await classifier.classify(
                text=text[:3000], filename=f["original_name"],
                mime_type=f.get("mime_type", ""), language="",
            )
            updates = {"category": result.category, "tags": result.tags, "summary": result.summary}
            await db.update_file(f["id"], **updates)
            count += 1
            log.info(f"Reclassified {f['id'][:8]} → {result.category}")
        except Exception as e:
            log.warning(f"Reclassify failed for {f['id'][:8]}: {e}")

    return RedirectResponse(f"/files?reclassified={count}", status_code=303)


@router.post("/files/{file_id}/reclassify")
async def reclassify_file(file_id: str):
    """Reclassify a single file via LLM."""
    import logging
    log = logging.getLogger(__name__)
    db = _get("db")
    classifier = _get("classifier")
    if not db or not classifier:
        return RedirectResponse(f"/files/{file_id}", status_code=303)

    file = await db.get_file(file_id)
    if not file:
        return RedirectResponse("/files", status_code=303)

    text = file.get("extracted_text", "") or ""
    if text.strip():
        try:
            result = await classifier.classify(
                text=text[:3000], filename=file["original_name"],
                mime_type=file.get("mime_type", ""), language="",
            )
            await db.update_file(file_id, category=result.category, tags=result.tags, summary=result.summary)
            log.info(f"Reclassified {file_id[:8]} → {result.category}")
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
    file_path = Path(file["stored_path"]).resolve()
    # Path traversal guard
    from app.config import get_settings as _gs
    base = _gs().storage.resolved_path
    if not file_path.is_relative_to(base):
        return HTMLResponse("Access denied", status_code=403)
    if not file_path.exists():
        return HTMLResponse("File not found on disk", status_code=404)
    mime = file.get("mime_type", "application/octet-stream")
    # For PDF and images: serve inline (in-browser view) by default
    previewable = mime in ("application/pdf",) or mime.startswith("image/")
    if previewable or inline:
        from starlette.responses import Response
        return Response(
            content=file_path.read_bytes(),
            media_type=mime,
            headers={"Content-Disposition": f'inline; filename="{file["original_name"]}"'},
        )
    return FileResponse(
        path=file_path,
        filename=file["original_name"],
        media_type=mime,
    )


@router.post("/files/{file_id}/delete")
async def file_delete(request: Request, file_id: str):
    """Cascading delete: file from disk + vectors from Qdrant + metadata from SQLite."""
    import logging
    logger = logging.getLogger(__name__)

    db = _get("db")
    file = await db.get_file(file_id) if db else None
    if not file:
        return RedirectResponse("/files", status_code=303)

    # 1. Delete vectors from Qdrant
    vector_store = _get("vector_store")
    if vector_store:
        try:
            await vector_store.delete_document(file_id)
            logger.info(f"Deleted vectors for {file_id}")
        except Exception as e:
            logger.warning(f"Failed to delete vectors for {file_id}: {e}")

    # 2. Delete file from disk
    file_storage = _get("file_storage")
    if file_storage and file.get("stored_path"):
        try:
            await file_storage.delete(Path(file["stored_path"]))
            logger.info(f"Deleted file from disk: {file['stored_path']}")
        except Exception as e:
            logger.warning(f"Failed to delete file from disk: {e}")

    # 3. Delete from SQLite (file record + processing logs + FTS)
    await db.delete_file(file_id)
    logger.info(f"Deleted file record from DB: {file_id}")

    return RedirectResponse("/files", status_code=303)


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


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    from app.config import get_settings
    from app.utils.crypto import mask_key
    import os
    settings = get_settings()
    vs = _get("vector_store")
    db = _get("db")
    qdrant_health = await vs.health_check() if vs else {}
    api_keys = await db.list_api_keys() if db else []
    # Get masked provider keys from env
    provider_keys = {
        "anthropic": mask_key(os.environ.get("ANTHROPIC_API_KEY", "")),
        "openai": mask_key(os.environ.get("OPENAI_API_KEY", "")),
        "google": mask_key(os.environ.get("GOOGLE_API_KEY", "")),
        "qdrant": mask_key(os.environ.get("QDRANT__API_KEY", settings.qdrant.api_key or "")),
    }
    pin_is_set = bool(await db.get_secret("PIN_HASH")) if db else False
    sensitive_count = 0
    if db:
        try:
            cur = await db.db.execute("SELECT COUNT(*) FROM files WHERE sensitive=1")
            row = await cur.fetchone()
            sensitive_count = row[0] if row else 0
        except Exception:
            sensitive_count = 0
    return templates.TemplateResponse("settings.html", {
        "request": request, "page": "settings", "settings": settings,
        "qdrant_health": qdrant_health, "api_keys": api_keys, "new_key": None,
        "provider_keys": provider_keys,
        "pin_is_set": pin_is_set,
        "sensitive_count": sensitive_count,
    })


@router.post("/settings/pin/set")
async def set_pin(request: Request):
    """Set or change the PIN that gates opening sensitive (encrypted) files."""
    from app.utils.crypto import hash_pin, verify_pin
    db = _get("db")
    form = await request.form()
    new_pin = (form.get("new_pin") or "").strip()
    confirm_pin = (form.get("confirm_pin") or "").strip()
    current_pin = (form.get("current_pin") or "").strip()

    if not new_pin.isdigit() or not (4 <= len(new_pin) <= 6):
        return RedirectResponse("/settings?pin=invalid", status_code=303)
    if new_pin != confirm_pin:
        return RedirectResponse("/settings?pin=mismatch", status_code=303)

    existing = await db.get_secret("PIN_HASH")
    if existing:
        # Changing existing PIN — require current.
        if not verify_pin(current_pin, existing):
            return RedirectResponse("/settings?pin=wrong_current", status_code=303)

    await db.set_secret("PIN_HASH", hash_pin(new_pin))
    return RedirectResponse("/settings?pin=ok", status_code=303)


@router.post("/settings/pin/clear")
async def clear_pin(request: Request):
    """Remove the PIN — sensitive files become unopenable from Telegram
    until a new PIN is set. The web login is the recovery path."""
    db = _get("db")
    await db.delete_secret("PIN_HASH")
    return RedirectResponse("/settings?pin=cleared", status_code=303)


@router.post("/settings/keys/save")
async def save_provider_keys(request: Request):
    """Save provider API keys (encrypted in SQLite)."""
    import os
    from app.config import get_settings
    from app.utils.crypto import encrypt
    db = _get("db")
    session_secret = get_settings().web.session_secret
    if not session_secret:
        return RedirectResponse("/settings", status_code=303)  # Can't encrypt without secret

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

    return RedirectResponse("/settings", status_code=303)


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

    settings = reload_settings()
    db = _get("db")
    vs = _get("vector_store")
    qdrant_health = await vs.health_check() if vs else {}
    api_keys = await db.list_api_keys() if db else []
    return templates.TemplateResponse("settings.html", {
        "request": request, "page": "settings", "settings": settings,
        "qdrant_health": qdrant_health, "api_keys": api_keys, "new_key": None,
        "prompt_saved": True,
    })


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

    # Presets per provider mode
    if provider_mode == "oauth":
        oauth_base = form.get("oauth_base", "http://127.0.0.1:10531/v1")
        for role in ("classification", "extraction", "search", "analysis"):
            model = form.get(f"model_{role}", "")
            if model:
                config["llm"]["models"][role] = {
                    "model": f"openai/{model}",
                    "api_base": oauth_base,
                    "api_key": "dummy",
                    "max_tokens": int(form.get(f"max_tokens_{role}", 1024)),
                    "temperature": float(form.get(f"temperature_{role}", 0.1)),
                }
    else:
        # API mode — use provider prefix, no api_base
        provider = form.get("api_provider", "anthropic")
        for role in ("classification", "extraction", "search", "analysis"):
            model = form.get(f"model_{role}", "")
            if model:
                entry = {
                    "model": model if "/" in model else f"{provider}/{model}",
                    "max_tokens": int(form.get(f"max_tokens_{role}", 1024)),
                    "temperature": float(form.get(f"temperature_{role}", 0.1)),
                }
                config["llm"]["models"][role] = entry

    config_path.write_text(yaml_lib.dump(config, default_flow_style=False, allow_unicode=True, sort_keys=False))
    settings = reload_settings()

    db = _get("db")
    vs = _get("vector_store")
    qdrant_health = await vs.health_check() if vs else {}
    api_keys = await db.list_api_keys() if db else []
    return templates.TemplateResponse("settings.html", {
        "request": request, "page": "settings", "settings": settings,
        "qdrant_health": qdrant_health, "api_keys": api_keys, "new_key": None,
        "llm_saved": True,
    })


@router.post("/settings/api-keys/create", response_class=HTMLResponse)
async def create_api_key(request: Request, name: str = Form("default"), mode: str = Form("lite")):
    from app.config import get_settings
    settings = get_settings()
    db = _get("db")
    vs = _get("vector_store")
    new_key = await db.create_api_key(name, mode=mode) if db else ""
    qdrant_health = await vs.health_check() if vs else {}
    api_keys = await db.list_api_keys() if db else []
    return templates.TemplateResponse("settings.html", {
        "request": request, "page": "settings", "settings": settings,
        "qdrant_health": qdrant_health, "api_keys": api_keys, "new_key": new_key,
    })


@router.post("/settings/api-keys/{key}/delete")
async def delete_api_key(key: str):
    db = _get("db")
    if db:
        await db.delete_api_key(key)
    return RedirectResponse("/settings", status_code=303)


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
        if file and file.get("stored_path"):
            try:
                p = Path(file["stored_path"])
                if p.exists():
                    p.unlink()
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
