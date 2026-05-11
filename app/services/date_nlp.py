"""Russian + English natural-language date parsing for Sprint P task
extraction and `/remind` commands.

Wraps `dateparser` with the settings that matter for this app:

* `PREFER_DATES_FROM='future'` — "в пятницу" should mean the upcoming
  Friday, not the previous one.
* `RETURN_AS_TIMEZONE_AWARE=False` — sqlite stores naive ISO strings.
* Languages limited to `ru` + `en` to skip the long autodetect chain.

`parse_due("через 2 часа", base=now())` → datetime two hours from now.
`parse_due("завтра в 9", base=now())` → tomorrow 09:00.
`parse_due("оплатить до 9 мая 2026")` → 2026-05-09 (the leading verb
is ignored; dateparser strips it).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

try:
    import dateparser
except ImportError:  # pragma: no cover
    dateparser = None  # type: ignore


_SETTINGS_BASE = {
    "PREFER_DATES_FROM": "future",
    "RETURN_AS_TIMEZONE_AWARE": False,
    "DATE_ORDER": "DMY",
}


_PREFIX_STRIP = re.compile(
    r"^(?:до|к|на|в|by|until|on|at|before)\s+", re.IGNORECASE
)


def parse_due(text: str, base: datetime | None = None) -> Optional[datetime]:
    if not text or dateparser is None:
        return None
    text = text.strip()
    if not text:
        return None
    settings = dict(_SETTINGS_BASE)
    if base is not None:
        settings["RELATIVE_BASE"] = base
    candidates = [text]
    stripped = _PREFIX_STRIP.sub("", text, count=1)
    if stripped and stripped != text:
        candidates.append(stripped)
    for c in candidates:
        try:
            dt = dateparser.parse(c, languages=["ru", "en"], settings=settings)
        except Exception:
            dt = None
        if dt is not None:
            return dt
    return None


def to_iso(dt: datetime | None) -> str:
    """Format as `YYYY-MM-DD HH:MM:SS` (sqlite-friendly)."""
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")
