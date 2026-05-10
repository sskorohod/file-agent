"""Wipe + redo every note_enrichments row from the now-decrypted note
content. The previous enrichments were generated when 155 of the 160
note rows held FAGE-encrypted base64, so the LLM saw garbage and
returned NULL/junk for mood/energy/sentiment. Run after the FAGE
decrypt sweep has cleaned `notes.content`.

Usage:
    .venv/bin/python scripts/redo_note_enrichments.py [--dry-run]

Cost: ~$0.03 for 159 notes through openai/gpt-5.4-mini via the
proxy. Stop uvicorn first to avoid SQLite WAL contention.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

for _k in ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL"):
    os.environ.pop(_k, None)
import dotenv
dotenv.load_dotenv("/Users/vskorokhod/fag/.env", override=True)

ROOT = "/Users/vskorokhod/fag"
sys.path.insert(0, ROOT)
os.chdir(ROOT)

PROMPT_VERSION = "v3-2026-05-10"

ENRICH_SYSTEM = """Ты система обогащения личных заметок дневника.

Тебе дают сырой текст заметки (русский / английский / смесь). Верни
JSON-объект со СТРОГИМИ полями для аналитики дашборда:

- suggested_title: короткий заголовок (≤ 50 символов), на языке заметки
- summary: 1-2 предложения о сути на русском
- category: одно из {personal, work, fitness, food, mood, sleep,
  symptom, idea, task, finance, family, other}
- subcategory: подкатегория (например для food: "breakfast" /
  "lunch" / "dinner" / "snack" / "drink"; для mood: "high" / "low";
  для sleep: "duration" / "quality")
- tags: 2-4 lowercase tag (eda, sleep, kofe, stress, idea, ...)
- mood_score: целое 1-10 (10=отлично, 1=ужасно). NULL если ничего
  про настроение не сказано
- energy: целое 1-10 (10=много энергии, 1=без сил). NULL если не упоминается
- sentiment: число -1.0 до 1.0 (положительность). Всегда число —
  даже у нейтральных «съел бутерброд» оценивается как 0.0
- confidence: 0.0-1.0 уверенность модели в своих оценках

ОТВЕТ — ТОЛЬКО JSON, никаких пояснений или markdown-блоков."""


async def main(dry_run: bool):
    import sqlite3
    import litellm

    db_path = f"{ROOT}/data/agent.db"
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row

    if not dry_run:
        # wipe — single canonical row per note
        con.execute("DELETE FROM note_enrichments")
        con.commit()

    # iterate notes with non-empty plaintext content
    rows = con.execute(
        "SELECT id, title, content, source, created_at FROM notes "
        "WHERE content != '' AND content NOT LIKE '%RkFHRQ%' "
        "ORDER BY id"
    ).fetchall()
    print(f"enriching {len(rows)} notes (dry={dry_run})\n")

    ok = fail = 0
    t0 = time.time()
    for i, r in enumerate(rows, 1):
        nid = r["id"]
        text = (r["content"] or "")[:2000]
        title = (r["title"] or "")
        user_msg = (
            f"[Заметка от {r['created_at'][:16]}, источник: {r['source']}]\n"
            f"{title}\n\n{text}"
        )
        try:
            resp = litellm.completion(
                model="openai/gpt-5.4-mini",
                api_base="http://127.0.0.1:10531/v1",
                api_key="dummy",
                max_tokens=400,
                temperature=0.1,
                messages=[
                    {"role": "system", "content": ENRICH_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            data = json.loads(raw)
        except Exception as e:
            print(f"[{i}/{len(rows)}] note {nid}: FAIL {e}")
            fail += 1
            continue

        if dry_run:
            print(f"[{i}/{len(rows)}] {nid}: {data.get('category','?'):8s} "
                  f"mood={data.get('mood_score')} energy={data.get('energy')} "
                  f"sent={data.get('sentiment')}")
            ok += 1
            continue

        try:
            con.execute(
                "INSERT INTO note_enrichments "
                "(note_id, suggested_title, summary, category, subcategory, "
                " tags, confidence, sentiment, energy, mood_score, "
                " raw_llm_json, model, prompt_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    nid,
                    (data.get("suggested_title") or "")[:80],
                    (data.get("summary") or "")[:500],
                    (data.get("category") or "other")[:30],
                    (data.get("subcategory") or "")[:30],
                    json.dumps(data.get("tags", []), ensure_ascii=False),
                    float(data.get("confidence") or 0.0),
                    None if data.get("sentiment") is None else float(data["sentiment"]),
                    None if data.get("energy") is None else int(data["energy"]),
                    None if data.get("mood_score") is None else int(data["mood_score"]),
                    raw[:5000],
                    "openai/gpt-5.4-mini",
                    PROMPT_VERSION,
                ),
            )
            # mirror to notes table for backwards-compat
            con.execute(
                "UPDATE notes SET sentiment=?, energy=?, mood_score=?, "
                "category=COALESCE(NULLIF(category,''), ?), "
                "subcategory=COALESCE(NULLIF(subcategory,''), ?), "
                "structured_json=?, processed_at=datetime('now'), status='enriched' "
                "WHERE id=?",
                (
                    None if data.get("sentiment") is None else str(float(data["sentiment"])),
                    None if data.get("energy") is None else str(int(data["energy"])),
                    None if data.get("mood_score") is None else str(int(data["mood_score"])),
                    (data.get("category") or "other"),
                    (data.get("subcategory") or ""),
                    json.dumps(data, ensure_ascii=False),
                    nid,
                ),
            )
            ok += 1
            if i % 20 == 0:
                con.commit()
                print(f"  ... {i} processed")
        except Exception as e:
            print(f"[{i}/{len(rows)}] note {nid}: write FAIL {e}")
            fail += 1
            continue

    if not dry_run:
        con.commit()
    con.close()
    print(f"\nDone in {time.time()-t0:.0f}s — ok={ok}, fail={fail}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(main(args.dry_run))
