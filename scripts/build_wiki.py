"""Build the LLM-wiki vault — Karpathy ``llm-wiki`` pattern applied to
the user's personal archive.

Layout under ``settings.wiki.base_path`` (default
``~/ai-agent-files/wiki``):

    CLAUDE.md          schema / conventions for any agent editing here
    index.md           top-level dashboard
    log.md             append-only ingest journal (latest 200 events)
    raw/               symlinks to the original files on disk
    docs/<slug>.md     one page per file (frontmatter + summary + link)
    notes/<date>-<slug>.md   one page per transcript
    entities/<slug>.md auto-extracted people / orgs / topics, backlinked

Every page carries YAML frontmatter (id, type, dates, sensitive flag,
tags, source path) and Obsidian-style ``[[wikilinks]]`` between docs,
notes and entities so the graph view connects them.

Idempotent — safe to rerun. Old `docs/` content from
build_docs_wiki.py supersedes this layout (the wiki vault is a new
sibling, not a replacement of the old per-doc cards). Make sure to
run ``make wiki-build`` after every reindex.

Usage:
    .venv/bin/python scripts/build_wiki.py [--no-entities]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import dotenv
dotenv.load_dotenv("/Users/vskorokhod/fag/.env", override=True)
ROOT = Path("/Users/vskorokhod/fag")
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


_SLUG_RE = re.compile(r"[^\w\s-]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def slug(text: str, max_len: int = 50) -> str:
    s = _SLUG_RE.sub("", text or "").strip()
    s = _WS_RE.sub("-", s).lower()
    return s[:max_len].strip("-") or "untitled"


def fm(d: dict) -> str:
    """Tiny YAML frontmatter (no nested objects, only scalars + lists)."""
    out = ["---"]
    for k, v in d.items():
        if v is None:
            out.append(f"{k}:")
        elif isinstance(v, bool):
            out.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, list):
            inner = ", ".join(json.dumps(x, ensure_ascii=False) for x in v)
            out.append(f"{k}: [{inner}]")
        elif isinstance(v, (int, float)):
            out.append(f"{k}: {v}")
        else:
            s = str(v).replace("\n", " ").replace('"', "'")
            if any(c in s for c in ":#[]{}|>&*!%@`,") or s.startswith(("- ", "? ")):
                out.append(f'{k}: "{s}"')
            else:
                out.append(f"{k}: {s}")
    out.append("---")
    return "\n".join(out)


# ── Entity extraction (lightweight, regex+heuristics, no LLM) ───────────────
# Pulls names, orgs, key concepts from `summary` and the first 1-2 KB of
# content / extracted_text. Heuristic: capitalised multi-word tokens that
# don't start a sentence; known org/abbreviation tokens (USCIS, IRS, DMV,
# DHS, HMRC, Kaiser Permanente, FAG, Cognee, …); tags from the row.
# This stays inside FAG's own runtime — no extra LLM cost. When cognee
# is healthy, a future revision can replace this with cognee.recall().

_ORG_HINTS = {
    "USCIS", "IRS", "DMV", "DHS", "FBI", "FDA", "Kaiser", "Permanente",
    "FAG", "Cognee", "Anthropic", "OpenAI", "Google", "Apple",
    "Telegram", "GitHub", "Karpathy", "Claude",
    "Covered California",
}
_PERSON_PATTERN = re.compile(
    r"\b([А-ЯA-Z][а-яa-zё]{1,}(?:\s+[А-ЯA-Z][а-яa-zё]{1,}){0,3})\b"
)


def _regex_entities(*texts: str, tags: list[str] | None = None) -> list[str]:
    """Fallback regex extractor (used when LLM is disabled / sidecar down)."""
    seen: dict[str, str] = {}
    for t in texts:
        if not t:
            continue
        for m in _PERSON_PATTERN.findall(t):
            if len(m) < 4:
                continue
            words = m.split()
            if len(words) >= 2 or any(h in m for h in _ORG_HINTS) or m.isupper():
                key = m.lower()
                seen.setdefault(key, m)
    if tags:
        for t in tags:
            if t and t.lower() not in seen:
                seen[t.lower()] = t
    return sorted(seen.values(), key=lambda x: (-len(x), x))[:25]


# Lazily instantiated EntityExtractor — set in main() when --use-llm is on.
_LLM_EXTRACTOR = None


async def extract_entities(
    *texts: str, tags: list[str] | None = None,
) -> list[str]:
    """Async LLM-driven entity extraction with regex fallback.

    When ``_LLM_EXTRACTOR`` is set (--use-llm path), each unique block of
    text gets one LLM call (cached by content sha256). Result is the
    canonical name from `entity_aliases`. Falls through to regex if the
    extractor is None or any LLM call raises.
    """
    if _LLM_EXTRACTOR is None:
        return _regex_entities(*texts, tags=tags)
    seen: dict[str, str] = {}
    blob = "\n\n".join(t for t in texts if t)
    if not blob.strip():
        return _regex_entities(*texts, tags=tags)
    try:
        ents = await _LLM_EXTRACTOR.extract(blob)
        for e in ents:
            seen.setdefault(e.name.lower(), e.name)
    except Exception:
        return _regex_entities(*texts, tags=tags)
    if tags:
        for t in tags:
            if t and t.lower() not in seen:
                seen[t.lower()] = t
    return sorted(seen.values(), key=lambda x: (-len(x), x))[:25]


# ── Main ────────────────────────────────────────────────────────────────────


async def main(no_entities: bool, use_llm: bool = False):
    from app.config import get_settings
    from app.storage.db import Database

    s = get_settings()
    vault = s.wiki.resolved_path
    print(f"vault: {vault}")
    for sub in ("docs", "notes", "entities", "raw"):
        (vault / sub).mkdir(parents=True, exist_ok=True)

    db = Database(s.database.path)
    await db.connect()

    # Sprint G — wire LLM extractor (optional). When enabled, replaces
    # the regex catch-all with proxy-driven (gpt-5.4-mini) extraction +
    # alias canonicalisation + content-hash cache. Costs ~$0.0002 per
    # source on first run, free thereafter.
    if use_llm and not no_entities:
        from app.llm.entities import EntityExtractor
        global _LLM_EXTRACTOR
        _LLM_EXTRACTOR = EntityExtractor(db)
        print("entity extractor: LLM (gpt-5.4-mini via proxy)")
    else:
        print("entity extractor: regex (legacy)")

    # ── 1. Schema page (CLAUDE.md) ──────────────────────────────────────
    (vault / "CLAUDE.md").write_text(_schema_md(s), encoding="utf-8")

    # ── 2. Files → docs/<slug>.md ───────────────────────────────────────
    cur = await db.db.execute(
        "SELECT id, original_name, stored_path, sha256, size_bytes, mime_type, "
        "category, tags, summary, source, metadata_json, sensitive, created_at, "
        "document_date FROM files ORDER BY created_at DESC"
    )
    files = [dict(r) for r in await cur.fetchall()]

    cur = await db.db.execute(
        "SELECT id, title, content, source, category, subcategory, tags, "
        "created_at, file_id FROM notes "
        "WHERE content != '' AND content NOT LIKE 'RkFHRQ%' "
        "ORDER BY created_at DESC"
    )
    notes = [dict(r) for r in await cur.fetchall()]

    seen_doc_slugs: dict[str, set[str]] = {}
    file_pages: dict[str, str] = {}  # file_id → relative_md path
    for f in files:
        cat = f.get("category") or "uncategorized"
        seen_doc_slugs.setdefault(cat, set())
        try:
            meta = json.loads(f.get("metadata_json") or "{}")
        except Exception:
            meta = {}
        try:
            tags = json.loads(f.get("tags") or "[]")
        except Exception:
            tags = []
        dtype = meta.get("document_type") or ""
        owner = meta.get("owner") or ""
        display = meta.get("display_label") or ""
        base = display or dtype or Path(f["original_name"]).stem
        sg = slug(base)
        if sg in seen_doc_slugs[cat]:
            sg = f"{sg}-{f['id'][:6]}"
        seen_doc_slugs[cat].add(sg)
        rel = f"docs/{cat}/{sg}.md"
        file_pages[f["id"]] = rel
        (vault / "docs" / cat).mkdir(parents=True, exist_ok=True)

        ent = (await extract_entities(
            f.get("summary", ""), owner, dtype,
            tags=tags,
        )) if not no_entities else []

        front = fm({
            "type": "document",
            "id": f["id"],
            "category": cat,
            "document_type": dtype,
            "owner": owner,
            "sensitive": bool(f.get("sensitive")),
            "expiry_date": meta.get("expiry_date") or "",
            "created_at": f.get("created_at") or "",
            "size_kb": round((f.get("size_bytes") or 0) / 1024, 1),
            "sha256": f.get("sha256") or "",
            "stored_path": f.get("stored_path") or "",
            "tags": tags,
            "entities": ent,
        })
        body_lines = [
            f"# {display or dtype or f['original_name']}",
            "",
            (f.get("summary") or "").strip(),
            "",
            "## Файл",
            "",
        ]
        sp = f.get("stored_path") or ""
        if sp:
            body_lines.append(f"📂 [{Path(sp).name}]({Path(sp).as_uri()})")
        if ent:
            body_lines.extend(["", "## Связи"])
            body_lines.extend(f"- [[entities/{slug(e)}|{e}]]" for e in ent[:10])
        body_lines.extend([
            "", "---", "",
            "> Автогенерация. Правки сюда — теряются при `make wiki-build`.",
        ])
        (vault / rel).write_text(
            front + "\n\n" + "\n".join(body_lines), encoding="utf-8",
        )

    # ── 3. Notes → notes/<date>-<slug>.md ───────────────────────────────
    note_pages: dict[int, str] = {}
    src_emoji = {"voice": "🎤", "text": "✍️", "telegram": "💬",
                 "checkin": "📊", "reminder": "🔔", "web": "🌐",
                 "file": "📎"}
    for n in notes:
        date_part = (n.get("created_at") or "")[:10]
        title = (n.get("title") or "").strip()
        body = (n.get("content") or "").strip()
        base = title or body.split("\n", 1)[0]
        sg = slug(base)
        rel = f"notes/{date_part}-{sg}.md"
        # Avoid filename clashes within the same day
        if (vault / rel).exists():
            rel = f"notes/{date_part}-{sg}-{n['id']}.md"
        note_pages[n["id"]] = rel

        try:
            tags = json.loads(n.get("tags") or "[]")
        except Exception:
            tags = []
        ent = (await extract_entities(title, body, tags=tags)) if not no_entities else []

        front = fm({
            "type": "note",
            "id": n["id"],
            "source": n.get("source") or "",
            "category": n.get("category") or "",
            "subcategory": n.get("subcategory") or "",
            "created_at": n.get("created_at") or "",
            "linked_file_id": n.get("file_id") or "",
            "tags": tags,
            "entities": ent,
        })
        emoji = src_emoji.get(n.get("source") or "", "•")
        ts = (n.get("created_at") or "")[:16]
        head = title or "Заметка"
        body_lines = [
            f"# {emoji} {head}",
            "",
            f"<i>{ts} · {n.get('source','')}</i>",
            "",
            body,
            "",
        ]
        # link to file if any
        fid = n.get("file_id") or ""
        if fid and fid in file_pages:
            body_lines.append(f"📎 Связан с [[{file_pages[fid].rsplit('.md',1)[0]}|документом]]")
        if ent:
            body_lines.extend(["", "## Связи"])
            body_lines.extend(
                f"- [[entities/{slug(e)}|{e}]]" for e in ent[:10]
            )
        body_lines.extend([
            "", "---", "",
            "> Автогенерация из notes table. Правки в `notes/` теряются при `make wiki-build`.",
        ])
        (vault / rel).write_text(
            front + "\n\n" + "\n".join(body_lines), encoding="utf-8",
        )

    # ── 4. Entities → entities/<slug>.md (with backlinks) ───────────────
    if not no_entities:
        # Aggregate: entity name → list of (kind, rel_md, title)
        entity_refs: dict[str, list[tuple[str, str, str]]] = {}
        for f in files:
            try:
                meta = json.loads(f.get("metadata_json") or "{}")
            except Exception:
                meta = {}
            try:
                tags = json.loads(f.get("tags") or "[]")
            except Exception:
                tags = []
            for e in (await extract_entities(
                f.get("summary", ""), meta.get("owner", ""),
                meta.get("document_type", ""), tags=tags,
            )):
                entity_refs.setdefault(e, []).append((
                    "document",
                    file_pages[f["id"]].rsplit(".md", 1)[0],
                    meta.get("display_label") or f["original_name"],
                ))
        for n in notes:
            try:
                tags = json.loads(n.get("tags") or "[]")
            except Exception:
                tags = []
            for e in (await extract_entities(
                n.get("title") or "", n.get("content") or "", tags=tags,
            )):
                entity_refs.setdefault(e, []).append((
                    "note",
                    note_pages[n["id"]].rsplit(".md", 1)[0],
                    (n.get("title") or n.get("content") or "")[:50],
                ))

        for ent_name, refs in entity_refs.items():
            sg = slug(ent_name)
            front = fm({
                "type": "entity",
                "name": ent_name,
                "kind": "auto",
                "mentions": len(refs),
            })
            body = [f"# {ent_name}", "",
                    f"_Упоминается в {len(refs)} источниках._", "",
                    "## Документы", ""]
            for kind, link, label in refs:
                if kind == "document":
                    body.append(f"- [[{link}|📄 {label}]]")
            body.extend(["", "## Заметки", ""])
            for kind, link, label in refs:
                if kind == "note":
                    body.append(f"- [[{link}|📝 {label}]]")
            (vault / "entities" / f"{sg}.md").write_text(
                front + "\n\n" + "\n".join(body), encoding="utf-8",
            )

    # ── 5. log.md ───────────────────────────────────────────────────────
    log_lines = [
        "# Лог индексации", "",
        f"_Собран {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}._", "",
    ]
    cur = await db.db.execute(
        "SELECT step, status, started_at, finished_at, file_id, "
        "substr(error,1,200) AS err FROM processing_log "
        "ORDER BY started_at DESC LIMIT 200"
    )
    for row in await cur.fetchall():
        r = dict(row)
        log_lines.append(
            f"- `{(r['started_at'] or '')[:16]}` **{r['step']}** "
            f"`{r['status']}` "
            f"{('— ' + r['err']) if r.get('err') else ''}"
        )
    (vault / "log.md").write_text("\n".join(log_lines), encoding="utf-8")

    # ── 6. index.md ─────────────────────────────────────────────────────
    cat_counts: dict[str, int] = {}
    for f in files:
        c = f.get("category") or "uncategorized"
        cat_counts[c] = cat_counts.get(c, 0) + 1
    notes_by_day: dict[str, int] = {}
    for n in notes:
        d = (n.get("created_at") or "")[:10]
        notes_by_day[d] = notes_by_day.get(d, 0) + 1

    idx = [
        "# 🧠 LLM-wiki",
        "",
        f"_Сгенерировано {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}._",
        "",
        "Это персональная wiki по архиву (Karpathy-паттерн). "
        "Каждый документ и каждая заметка имеют отдельную страницу с "
        "frontmatter и backlink-ами. Сущности (люди / организации / "
        "темы) автоматически выделяются и связывают всё.",
        "",
        "## Документы", "",
    ]
    for cat in sorted(cat_counts, key=lambda c: -cat_counts[c]):
        idx.append(f"- **{cat}** — {cat_counts[cat]} (см. `docs/{cat}/`)")
    idx.append("")
    idx.append("## Заметки по дням")
    idx.append("")
    for day in sorted(notes_by_day, reverse=True)[:30]:
        idx.append(f"- `{day}` — {notes_by_day[day]} заметок")
    idx.extend([
        "", "## Сущности", "",
        f"См. `entities/` ({len(list((vault/'entities').glob('*.md')))} файлов).",
        "", "## Журнал", "",
        "См. `log.md` (последние 200 операций индексации).",
        "", "## Сырые файлы",
        "",
        f"См. `raw/` (символические ссылки на оригиналы под "
        f"`{s.storage.base_path}`).",
    ])
    (vault / "index.md").write_text("\n".join(idx), encoding="utf-8")

    # ── 7. raw/ — symlinks to actual stored files (idempotent) ──────────
    raw_dir = vault / "raw"
    for f in files:
        sp = f.get("stored_path") or ""
        if not sp:
            continue
        target = Path(sp)
        if not target.exists():
            continue
        link = raw_dir / f"{f['id'][:8]}-{Path(sp).name}"
        if link.exists() or link.is_symlink():
            continue
        try:
            link.symlink_to(target)
        except Exception:
            pass

    print(f"✓ wrote: {len(files)} docs, {len(notes)} notes, "
          f"{len(list((vault/'entities').glob('*.md')))} entities")


def _schema_md(settings) -> str:
    return f"""# Wiki schema (auto-readable by agents)

This vault is a Karpathy-style **LLM-wiki**. Every page has YAML
frontmatter and Obsidian-compatible `[[wikilinks]]`. The vault is
**rebuilt** by `scripts/build_wiki.py`; manual edits inside `docs/`,
`notes/` and `entities/` will be overwritten on the next run. Persistent
notes go either into the source-of-truth table (`notes` in SQLite) or
into a sibling `manual/` directory, which the builder leaves alone.

## File kinds

| Path | `frontmatter.type` | Source of truth |
|---|---|---|
| `docs/<category>/<slug>.md` | `document` | `files` table |
| `notes/<YYYY-MM-DD>-<slug>.md` | `note` | `notes` table |
| `entities/<slug>.md` | `entity` | derived from doc/note text |
| `log.md` | — | `processing_log` table |
| `index.md` | — | aggregate dashboard |
| `raw/<id>-<filename>` | symlink | the file on disk |

## Frontmatter conventions

Always present:

```yaml
type: document | note | entity
id:   <stable id>           # files.id, notes.id, or slug for entities
created_at: 2026-05-10 ...  # ISO-ish UTC
```

Documents add: `category`, `document_type`, `owner`, `sensitive`,
`expiry_date`, `size_kb`, `sha256`, `stored_path`, `tags[]`,
`entities[]`.

Notes add: `source`, `category`, `subcategory`, `linked_file_id`,
`tags[]`, `entities[]`.

Entities add: `name`, `kind` (auto / manual), `mentions`.

## Linking

- A document or note links to entities it mentions:
  `[[entities/<slug>|<Name>]]`.
- An entity page lists every doc + note that mentions it.
- A note linked to a specific doc carries `linked_file_id` in
  frontmatter and a `[[docs/...|документ]]` body link.

## Editing rules for an LLM agent

1. **Never edit autogen files** — `docs/*`, `notes/*`, `entities/*`,
   `index.md`, `log.md`. Rebuild with `make wiki-build`.
2. **Manual notes / pages** go under `manual/` (create on demand).
3. **Source of truth is SQLite** — edit there if you want a real
   change to survive rebuilds.
4. **Wikilinks must be vault-relative**, no leading `/`.
5. **Frontmatter scalars only** — no nested objects.

## Vault path

Configurable via `wiki.base_path` in `config.yaml` or `WIKI__BASE_PATH`
env. Currently: `{settings.wiki.base_path}` (resolved:
`{settings.wiki.resolved_path}`).
"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-entities", action="store_true",
                    help="skip entity extraction (faster, but no graph)")
    ap.add_argument("--use-llm", action="store_true",
                    help="use the LLM entity extractor (Sprint G); "
                         "default is regex fallback for cheap/offline runs")
    args = ap.parse_args()
    asyncio.run(main(args.no_entities, args.use_llm))
