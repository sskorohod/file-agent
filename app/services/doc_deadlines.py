"""Sprint P — regex-based deadline extractor for ingested documents.

Three patterns matter for the user's actual document mix:

* USCIS / immigration fee notices ("оплатить до …", "must be paid by …",
  "biometrics appointment …", "interview …")
* Medical: appointments, prescription refills
* Bills, tax forms, invoices ("due date …", "оплатить до …")

Returns a list of `{remind_at, message}` dicts. The caller writes them
into the `reminders` table; the existing `_reminder_loop` already pushes
those to Telegram. We aim for `remind_at = deadline - 1 day`.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from app.services.date_nlp import parse_due, to_iso

logger = logging.getLogger(__name__)


# Regexes that pull out a "deadline-y" clause + the date phrase.
# Each pattern's group(1) is the date phrase fed to dateparser.
_PAT_RU = [
    re.compile(r"оплат\w*\s+(?:до|к)\s+([0-9]{1,2}[\.\s\-/][а-я]+(?:[\s\-/][0-9]{2,4})?)", re.IGNORECASE),
    re.compile(r"оплат\w*\s+(?:до|к)\s+([0-9]{1,2}[\./\-][0-9]{1,2}[\./\-][0-9]{2,4})", re.IGNORECASE),
    re.compile(r"приём\s+(?:у\s+врача\s+)?([0-9]{1,2}[\.\s\-/][а-я]+(?:[\s\-/][0-9]{2,4})?)", re.IGNORECASE),
    re.compile(r"запис\w+\s+на\s+([0-9]{1,2}[\.\s\-/][а-я]+(?:[\s\-/][0-9]{2,4})?)", re.IGNORECASE),
    re.compile(r"срок\s+(?:до|истекает)\s+([0-9]{1,2}[\.\s\-/][а-я]+(?:[\s\-/][0-9]{2,4})?)", re.IGNORECASE),
]
_PAT_EN = [
    re.compile(r"\b(?:pay|paid|payment|fee|deadline|deliver(?:ed)?|complete(?:d)?|submit(?:ted)?)\b[^.\n]{0,40}?\bby\s+([A-Za-z]+\.?\s+[0-9]{1,2},?\s*[0-9]{4})", re.IGNORECASE),
    re.compile(r"\bdue\s+(?:date|by|on)?\s*:?\s*([A-Za-z]+\.?\s+[0-9]{1,2},?\s*[0-9]{4})", re.IGNORECASE),
    re.compile(r"\bdue\s+(?:date|by|on)?\s*:?\s*([0-9]{1,2}[\./\-][0-9]{1,2}[\./\-][0-9]{2,4})", re.IGNORECASE),
    re.compile(r"\b(?:appointment|interview|biometrics)\s+(?:is\s+)?(?:on\s+|scheduled\s+for\s+)?([A-Za-z]+\.?\s+[0-9]{1,2},?\s*[0-9]{4})", re.IGNORECASE),
]


_RELEVANT_CATEGORIES = {
    "immigration", "uscis", "medical", "health", "finance", "tax",
    "bills", "invoice", "legal",
}


def extract_deadlines(text: str, category: str, doc_type: str) -> list[dict]:
    if not text:
        return []
    cat_l = (category or "").lower()
    type_l = (doc_type or "").lower()
    if cat_l not in _RELEVANT_CATEGORIES and not any(
        kw in type_l for kw in ("invoice", "bill", "fee", "appointment",
                                 "uscis", "tax", "medical", "prescription")
    ):
        # Still scan if "оплатить до" appears literally — small extra net.
        if not re.search(r"оплат\w+\s+до", text, re.IGNORECASE) \
           and not re.search(r"\bdue\s+(?:date|by)\b", text, re.IGNORECASE):
            return []

    now = datetime.now()
    out: list[dict] = []
    seen: set[str] = set()
    for pat in _PAT_RU + _PAT_EN:
        for m in pat.finditer(text):
            phrase = m.group(1).strip()
            dt = parse_due(phrase, base=now)
            if dt is None:
                # MDY fallback for US-style "06/15/2027" — dateparser
                # gives up on slash dates that violate the configured
                # DATE_ORDER, so retry with the parts swapped.
                slash_m = re.match(r"^(\d{1,2})[\./\-](\d{1,2})[\./\-](\d{2,4})$", phrase)
                if slash_m:
                    a, b, y = slash_m.groups()
                    dt = parse_due(f"{b}/{a}/{y}", base=now)
            if dt is None or dt <= now:
                continue
            iso = to_iso(dt - timedelta(days=1))
            if iso in seen:
                continue
            seen.add(iso)
            ctx = text[max(0, m.start() - 30): m.end() + 30].replace("\n", " ").strip()
            out.append({
                "remind_at": iso,
                "message": f"Дедлайн {dt.strftime('%Y-%m-%d')}: {ctx[:160]}",
            })
            if len(out) >= 5:
                return out
    return out
