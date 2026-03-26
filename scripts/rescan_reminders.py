"""Rescan all files: re-extract expiry_date via LLM and recreate reminders.

Reminder lead times:
  Passport:        180 days (6 months)
  Driver license:   60 days
  Other:            14 days (2 weeks)

Usage:
    python -m scripts.rescan_reminders
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from app.config import get_settings
from app.storage.db import Database
from app.llm.router import LLMRouter
from app.skills.engine import SkillEngine
from app.pipeline import _reminder_days_for_doc_type
from app.parser.factory import ParserFactory

logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y", "%d/%m/%Y")


def parse_date(s: str) -> datetime | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


async def extract_expiry(llm: LLMRouter, text: str, doc_type: str) -> dict | None:
    """Ask LLM to extract expiry_date and document_type from text."""
    system = (
        "Extract document details. Return ONLY valid JSON:\n"
        '{"document_type": "<type>", "expiry_date": "<YYYY-MM-DD or empty>"}\n\n'
        "Rules:\n"
        "- document_type: passport, driver_license, visa, insurance, license, certificate, etc.\n"
        "- expiry_date: the expiration/validity date in YYYY-MM-DD format. Empty string if none found.\n"
        "- Look for dates labeled: expires, valid until, expiration, срок действия, действителен до\n"
    )
    try:
        response = await llm.extract(text=text[:3000], system=system)
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(raw)
    except Exception as e:
        logger.debug(f"LLM extraction failed: {e}")
        return None


async def main():
    settings = get_settings()
    settings.setup_env_keys()
    db = Database(settings.database.path)
    await db.connect()

    llm = LLMRouter(settings.llm)
    parser_factory = ParserFactory()
    skills = SkillEngine(settings.skills.directory)
    await skills.load_all()

    files = await db.list_files(limit=10000)
    logger.info(f"Total files: {len(files)}")

    # Clear old auto-reminders
    await db.db.execute("DELETE FROM reminders WHERE message LIKE '%Срок действия истекает%'")
    await db.db.commit()
    logger.info("Cleared old auto-reminders")

    created = 0
    skipped = 0
    errors = 0

    for f in files:
        meta = {}
        if f.get("metadata_json"):
            try:
                meta = json.loads(f["metadata_json"])
            except (json.JSONDecodeError, TypeError):
                continue

        extracted = meta.get("extracted_fields", {})
        expiry_str = extracted.get("expiry_date", "")
        doc_type = extracted.get("document_type", "") or meta.get("document_type", "") or ""

        # If no expiry_date extracted yet, try to re-extract via LLM
        if not expiry_str:
            stored_path = f.get("stored_path", "")
            extracted_text = f.get("extracted_text", "")

            if not extracted_text and stored_path and Path(stored_path).exists():
                try:
                    pr = await parser_factory.parse(Path(stored_path))
                    extracted_text = pr.text or ""
                except Exception as e:
                    logger.warning(f"  Parse failed {f['original_name']}: {e}")

            if not extracted_text:
                skipped += 1
                continue

            logger.info(f"  Re-extracting: {f['original_name']} (type={doc_type})")
            result = await extract_expiry(llm, extracted_text, doc_type)
            if result and result.get("expiry_date"):
                expiry_str = result["expiry_date"]
                if result.get("document_type"):
                    doc_type = result["document_type"]
                # Update metadata
                extracted["expiry_date"] = expiry_str
                if result.get("document_type"):
                    extracted["document_type"] = result["document_type"]
                meta["extracted_fields"] = extracted
                await db.db.execute(
                    "UPDATE files SET metadata_json=? WHERE id=?",
                    (json.dumps(meta, ensure_ascii=False), f["id"]),
                )
                await db.db.commit()
                logger.info(f"    Found expiry: {expiry_str}, type: {doc_type}")
            else:
                skipped += 1
                continue

        # Parse and create reminder
        exp_date = parse_date(expiry_str)
        if not exp_date:
            logger.warning(f"  Cannot parse date '{expiry_str}' for {f['original_name']}")
            errors += 1
            continue

        remind_days = _reminder_days_for_doc_type(doc_type)
        remind_date = exp_date - timedelta(days=remind_days)

        if remind_date <= datetime.now():
            logger.info(
                f"  PAST {f['original_name']}: "
                f"expires {expiry_str}, would remind {remind_date.date()} ({remind_days}d before)"
            )
            skipped += 1
            continue

        message = f"Срок действия истекает: {expiry_str} (напоминание за {remind_days} дн.)"
        await db.create_reminder(
            file_id=f["id"],
            remind_at=remind_date.isoformat(),
            message=message,
        )
        created += 1
        logger.info(
            f"  REMINDER {f['original_name']}: "
            f"type={doc_type}, expires={expiry_str}, "
            f"remind={remind_date.date()} ({remind_days}d before)"
        )

    await db.close()
    print(f"\nDone: {created} reminders created, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    asyncio.run(main())
