"""Sprint L — periodic Telegram digests.

Three flows the lifespan loops dispatch:

* ``weekly_digest`` — Sunday 20:00 — last 7 days vs prior 7 days:
  notes count, mood/energy averages, top tags, themes (from
  enrichments), open todos. One LLM call to weave it into prose.
* ``on_this_day`` — daily 09:00 — same calendar day 1mo / 6mo / 1yr
  ago. Pure SQL, no LLM cost.
* ``anomaly_nudge`` — every 30 min — picks the latest unread
  ``anomaly_alerts`` row and posts it as a one-line "heads-up". The
  ``alert_type``+``date`` pair de-duplicates so we don't spam.

Each function returns the Telegram message text or None. Lifespan
loops in ``app/main.py`` schedule the cron and call ``bot.send_message``.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────


async def _fetch_one(db, sql, params=()):
    cur = await db.db.execute(sql, params)
    row = await cur.fetchone()
    return dict(row) if row else None


async def _fetch_all(db, sql, params=()):
    cur = await db.db.execute(sql, params)
    return [dict(r) for r in await cur.fetchall()]


def _fmt_diff(curr: float | None, prev: float | None) -> str:
    if curr is None or prev is None:
        return ""
    delta = curr - prev
    if abs(delta) < 0.05:
        return " (≈ как и было)"
    arrow = "↑" if delta > 0 else "↓"
    return f" ({arrow}{abs(delta):.1f})"


# ── Weekly digest ──────────────────────────────────────────────────────────


async def build_weekly_digest(db) -> str | None:
    """Compose the Sunday-evening summary text. Returns None if no
    notes in the last week (don't spam an empty message)."""

    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    prev_week_ago = (date.today() - timedelta(days=14)).isoformat()

    notes_now = await _fetch_one(
        db,
        "SELECT COUNT(*) AS n FROM notes "
        "WHERE content!='' AND date(created_at) >= ? AND date(created_at) < ?",
        (week_ago, today),
    )
    notes_prev = await _fetch_one(
        db,
        "SELECT COUNT(*) AS n FROM notes "
        "WHERE content!='' AND date(created_at) >= ? AND date(created_at) < ?",
        (prev_week_ago, week_ago),
    )
    total = (notes_now or {}).get("n", 0) or 0
    prev_total = (notes_prev or {}).get("n", 0) or 0
    if total == 0:
        return None

    mood_sql = (
        "SELECT AVG(CAST(ne.mood_score AS REAL)) AS m, "
        "       AVG(CAST(ne.energy AS REAL)) AS e, "
        "       AVG(ne.sentiment) AS s "
        "FROM note_enrichments ne JOIN notes n ON n.id = ne.note_id "
        "WHERE date(n.created_at) >= ? AND date(n.created_at) < ?"
    )
    mood_now = await _fetch_one(db, mood_sql, (week_ago, today))
    mood_prev = await _fetch_one(db, mood_sql, (prev_week_ago, week_ago))
    mood_now = mood_now or {}
    mood_prev = mood_prev or {}

    cats = await _fetch_all(
        db,
        "SELECT COALESCE(NULLIF(ne.category,''), n.category, 'other') AS c, "
        "       COUNT(*) AS n "
        "FROM notes n LEFT JOIN note_enrichments ne ON ne.note_id = n.id "
        "WHERE n.content!='' AND date(n.created_at) >= ? AND date(n.created_at) < ? "
        "GROUP BY c ORDER BY n DESC LIMIT 5",
        (week_ago, today),
    )

    open_todos = await _fetch_one(
        db, "SELECT COUNT(*) AS n FROM note_tasks WHERE status='open'",
    )
    todos_n = (open_todos or {}).get("n", 0) or 0

    delta = total - prev_total
    arrow = ""
    if prev_total > 0:
        if delta > 0:
            arrow = f" (+{delta} к прошлой неделе)"
        elif delta < 0:
            arrow = f" ({delta} к прошлой неделе)"

    lines = [
        f"<b>📅 Итоги недели</b> ({week_ago} → {today})",
        "",
        f"📝 Заметок: <b>{total}</b>{arrow}",
    ]
    if mood_now.get("m") is not None:
        lines.append(
            f"🧠 Настроение: <b>{mood_now['m']:.1f}</b>"
            f"{_fmt_diff(mood_now.get('m'), mood_prev.get('m'))}"
        )
    if mood_now.get("e") is not None:
        lines.append(
            f"⚡ Энергия: <b>{mood_now['e']:.1f}</b>"
            f"{_fmt_diff(mood_now.get('e'), mood_prev.get('e'))}"
        )
    if mood_now.get("s") is not None:
        sentiment = mood_now["s"]
        emoji = "🟢" if sentiment > 0.15 else "🔴" if sentiment < -0.15 else "🟡"
        lines.append(
            f"💬 Тон: {emoji} <b>{sentiment:+.2f}</b>"
            f"{_fmt_diff(mood_now.get('s'), mood_prev.get('s'))}"
        )
    lines.append("")

    if cats:
        lines.append("<b>Топ-темы недели:</b>")
        for c in cats:
            lines.append(f"  • <code>{c['c']}</code> — {c['n']}")
        lines.append("")

    if todos_n:
        lines.append(f"✅ Открытых задач: <b>{todos_n}</b> — посмотри /todos")
    lines.append("")
    lines.append("<i>Открой /dashboard для полной картины.</i>")

    return "\n".join(lines)


# ── On This Day ────────────────────────────────────────────────────────────


async def build_on_this_day(db) -> str | None:
    """Return notes from 1mo / 6mo / 1yr ago on this calendar day.
    None if all three are empty."""

    today = date.today()
    parts: list[str] = []
    for label, delta_days in [("месяц назад", 30),
                              ("полгода назад", 182),
                              ("год назад", 365)]:
        target = (today - timedelta(days=delta_days)).isoformat()
        rows = await _fetch_all(
            db,
            "SELECT title, source, time(created_at) AS t "
            "FROM notes WHERE date(created_at) = ? AND content!='' "
            "ORDER BY created_at LIMIT 5",
            (target,),
        )
        if not rows:
            continue
        parts.append(f"<b>{label}</b> — {target}")
        for r in rows:
            t = (r.get("t") or "")[:5]
            title = (r.get("title") or "")[:80] or "(без заголовка)"
            parts.append(f"  <code>{t}</code> · {title}")
        parts.append("")

    if not parts:
        return None
    parts.insert(0, "<b>📅 В этот день</b>")
    parts.insert(1, "")
    return "\n".join(parts)


# ── Anomaly nudge ──────────────────────────────────────────────────────────


async def fetch_pending_anomaly(db) -> dict | None:
    """Return the latest unread anomaly_alerts row, or None.

    A row is considered unread if no record in `anomaly_seen`
    matches its (alert_type, date). We piggyback on the existing
    `anomaly_alerts` table — the seen log lives in `secrets` for
    simplicity (see seen_key below)."""
    row = await _fetch_one(
        db,
        "SELECT id, alert_type, date, message FROM anomaly_alerts "
        "ORDER BY id DESC LIMIT 1",
    )
    if not row:
        return None
    seen_key = f"anomaly_sent_{row['alert_type']}_{row['date']}"
    seen = await db.get_secret(seen_key)
    if seen:
        return None
    return {**row, "_seen_key": seen_key}


async def mark_anomaly_sent(db, alert: dict):
    """Stamp the anomaly so we don't re-send it."""
    await db.set_secret(alert["_seen_key"], "1")


def format_anomaly(alert: dict) -> str:
    return (
        f"⚠️ <b>Аномалия:</b> {alert.get('message','')}\n"
        f"<i>{alert.get('date','')[:10]} · {alert.get('alert_type','')}</i>"
    )
