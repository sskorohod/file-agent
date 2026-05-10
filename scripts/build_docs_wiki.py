"""Build an Obsidian-friendly wiki of every document in the archive.

For each row in `files`, writes a markdown page at
    <vault>/docs/<category>/<slug>.md
with YAML frontmatter (id, type, owner, dates, sensitive, tags, file path)
and a body containing the summary, a clickable link to the on-disk file,
and back-references to any notes that already mention this document.

Also writes a top-level <vault>/_DOCS.md index — a table grouped by
category, one row per document.

The vault root is the FileStorage base path
(`/Users/vskorokhod/ai-agent-files` by default), so the existing
`notes/` tree and the new `docs/` tree share one Obsidian vault. Open
the folder in Obsidian and the wikilinks resolve.

Idempotent — rerun to refresh after a reindex or new ingest.

Usage:
    .venv/bin/python scripts/build_docs_wiki.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import dotenv
dotenv.load_dotenv("/Users/vskorokhod/fag/.env", override=True)
ROOT = Path("/Users/vskorokhod/fag")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def _slug(s: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE).strip()
    s = re.sub(r"\s+", "-", s)
    return s[:max_len].strip("-") or "untitled"


def _yaml_value(v):
    """Quote and escape a value for YAML frontmatter — keep it simple."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("\n", " ").replace('"', "'")
    return f'"{s}"' if any(c in s for c in ":#[]{}|>&*!%@`,") else s


async def main():
    from app.config import get_settings

    s = get_settings()
    vault = Path(s.storage.base_path).expanduser().resolve()
    docs_root = vault / "docs"
    docs_root.mkdir(parents=True, exist_ok=True)
    print(f"vault root: {vault}")
    print(f"docs root:  {docs_root}\n")

    from app.storage.db import Database

    db = Database(s.database.path)
    await db.connect()

    cur = await db.db.execute(
        "SELECT id, original_name, stored_path, sha256, size_bytes, mime_type, "
        "category, tags, summary, source, metadata_json, sensitive, created_at, "
        "document_date FROM files ORDER BY created_at"
    )
    files = [dict(r) for r in await cur.fetchall()]

    # Backref map: file_id → list of (note_title, note_md_path)
    cur = await db.db.execute(
        "SELECT title, file_id, md_path FROM notes WHERE file_id != ''"
    )
    backrefs: dict[str, list[tuple[str, str]]] = {}
    for r in await cur.fetchall():
        rd = dict(r)
        backrefs.setdefault(rd["file_id"], []).append(
            (rd.get("title") or "", rd.get("md_path") or "")
        )

    # Track which slugs we've used per category (collision avoidance)
    seen: dict[str, set[str]] = {}
    written: list[dict] = []

    for f in files:
        cat = f.get("category", "uncategorized") or "uncategorized"
        cat_dir = docs_root / cat
        cat_dir.mkdir(parents=True, exist_ok=True)
        seen.setdefault(cat, set())

        meta = {}
        try:
            meta = json.loads(f.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        doc_type = meta.get("document_type") or ""
        expiry = meta.get("expiry_date") or ""
        language = meta.get("language") or ""
        reindexed = meta.get("reindexed_at") or ""
        try:
            tags = json.loads(f.get("tags") or "[]")
        except Exception:
            tags = []

        original = f["original_name"]
        base = doc_type or Path(original).stem
        slug = _slug(base)
        if slug in seen[cat]:
            slug = f"{slug}-{f['id'][:6]}"
        seen[cat].add(slug)

        md_path = cat_dir / f"{slug}.md"
        stored = f.get("stored_path") or ""
        # Obsidian-friendly relative file link (no URL-encoding needed for
        # `file://` since we're inside the vault, but use absolute path).
        file_link = f"[📂 Открыть оригинал]({Path(stored).as_uri()})" if stored else ""
        size_kb = round((f.get("size_bytes") or 0) / 1024, 1)

        fm_lines = [
            "---",
            f"id: {f['id']}",
            f"original: {_yaml_value(original)}",
            f"category: {_yaml_value(cat)}",
            f"document_type: {_yaml_value(doc_type)}",
            f"sensitive: {_yaml_value(bool(f.get('sensitive')))}",
            f"created_at: {_yaml_value(f.get('created_at',''))}",
            f"document_date: {_yaml_value(f.get('document_date',''))}",
            f"expiry_date: {_yaml_value(expiry)}",
            f"language: {_yaml_value(language)}",
            f"sha256: {_yaml_value(f.get('sha256',''))}",
            f"size_bytes: {f.get('size_bytes', 0)}",
            f"mime_type: {_yaml_value(f.get('mime_type',''))}",
            f"source: {_yaml_value(f.get('source',''))}",
            f"stored_path: {_yaml_value(stored)}",
            f"reindexed_at: {_yaml_value(reindexed)}",
            f"tags: [{', '.join(tags)}]",
            "---",
            "",
        ]

        body = []
        sensitivity = "🔒 Sensitive" if f.get("sensitive") else "📄 Public"
        title = doc_type or original
        body.append(f"# {title}")
        body.append("")
        body.append(f"**{sensitivity}** · 📁 `{cat}` · 🆔 `{f['id'][:8]}` · "
                    f"💾 `{size_kb} KB`")
        if expiry:
            body.append(f"⏰ Действует до: **{expiry}**")
        body.append("")
        if f.get("summary"):
            body.append("## Описание")
            body.append("")
            body.append(f.get("summary"))
            body.append("")
        body.append("## Файл")
        body.append("")
        if file_link:
            body.append(file_link)
        body.append(f"- 📁 Папка: `{Path(stored).parent}`" if stored else "")
        body.append(f"- 📝 Имя на диске: `{Path(stored).name}`" if stored else "")
        body.append(f"- ⤵️ Загружен: `{f.get('created_at','')}`")
        body.append(f"- 📨 Источник: `{f.get('source','')}`")
        body.append("")
        if tags:
            body.append("## Теги")
            body.append("")
            body.append(" ".join(f"#{t.replace(' ', '_')}" for t in tags))
            body.append("")
        refs = backrefs.get(f["id"], [])
        if refs:
            body.append("## Связанные заметки")
            body.append("")
            for title2, md in refs:
                # Convert absolute md_path → vault-relative wikilink
                if md:
                    rel = Path(md).relative_to(vault) if str(md).startswith(str(vault)) \
                          else Path(md).name
                    rel_no_ext = str(rel).rsplit(".md", 1)[0]
                    body.append(f"- [[{rel_no_ext}|{title2 or rel_no_ext}]]")
                else:
                    body.append(f"- {title2}")
            body.append("")
        body.append("---")
        body.append("")
        body.append("> Этот файл сгенерирован автоматически (`scripts/build_docs_wiki.py`). "
                    "Правки сюда не нужны — они перезапишутся при следующем reindex/refresh.")

        md_path.write_text(
            "\n".join(fm_lines + body), encoding="utf-8"
        )
        written.append({
            "id": f["id"], "category": cat, "doc_type": doc_type,
            "original": original, "expiry": expiry, "sensitive": bool(f.get("sensitive")),
            "summary": f.get("summary") or "", "size_kb": size_kb,
            "rel_md": md_path.relative_to(vault),
            "created_at": f.get("created_at", ""),
        })

    # ── Top-level index ─────────────────────────────────────────────────
    by_cat: dict[str, list[dict]] = {}
    for w in written:
        by_cat.setdefault(w["category"], []).append(w)

    idx = [
        "# 📚 Архив документов",
        "",
        f"_Автогенерировано: всего **{len(written)}** документов в **{len(by_cat)}** категориях._",
        "",
        "Открой любой документ кликом по 🔗. Каждая страница содержит описание, "
        "ссылку на оригинал и связанные заметки.",
        "",
    ]
    cat_order = ["personal", "business", "health"]
    cats = [c for c in cat_order if c in by_cat] + sorted(
        c for c in by_cat if c not in cat_order
    )
    for cat in cats:
        rows = sorted(by_cat[cat], key=lambda w: w["created_at"], reverse=True)
        idx.append(f"## {cat} ({len(rows)})")
        idx.append("")
        idx.append("| 🔗 | Тип | Оригинал | 📅 | 🔒 | Описание |")
        idx.append("|---|---|---|---|---|---|")
        for w in rows:
            link = f"[[{str(w['rel_md']).rsplit('.md', 1)[0]}|открыть]]"
            type_ = (w["doc_type"] or "—")[:40]
            orig = w["original"][:40]
            date = w["created_at"][:10] if w["created_at"] else ""
            sens = "🔒" if w["sensitive"] else ""
            sumr = (w["summary"] or "").replace("|", " ").replace("\n", " ")[:90]
            idx.append(f"| {link} | {type_} | `{orig}` | {date} | {sens} | {sumr} |")
        idx.append("")

    (vault / "_DOCS.md").write_text("\n".join(idx), encoding="utf-8")
    print(f"wrote {len(written)} doc pages + _DOCS.md index")


if __name__ == "__main__":
    asyncio.run(main())
