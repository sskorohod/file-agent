"""Dashboard PNG generator — turns SQLite metrics into a single
multi-panel PNG that the Telegram bot sends as a photo.

Used by `/dashboard` and `/today` commands. Pure-server: no internet
calls (matplotlib + sqlite + io.BytesIO), so charts render even when
the LLM proxy is down.

Public API:

    async build_dashboard_png(db, days: int = 30) -> bytes
    async build_today_png(db) -> bytes

Note on emoji: matplotlib's default font cascade on macOS doesn't
include color emoji, so they render as tofu boxes. We keep titles
ASCII/Cyrillic-only and let Telegram caption carry the emoji
prefix instead.
"""
from __future__ import annotations

from datetime import date, datetime
from io import BytesIO

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt    # noqa: E402

# --- styling ----------------------------------------------------------------

_BG = "#0f172a"
_FG = "#e2e8f0"
_GRID = "#1e293b"
_ACCENT = "#38bdf8"
_GOOD = "#34d399"
_WARN = "#fbbf24"
_BAD = "#f87171"
_PURPLE = "#a78bfa"

plt.rcParams.update({
    "figure.facecolor": _BG, "axes.facecolor": _BG,
    "axes.edgecolor": _GRID, "axes.labelcolor": _FG,
    "axes.titlecolor": _FG, "text.color": _FG,
    "xtick.color": _FG, "ytick.color": _FG,
    "grid.color": _GRID, "font.size": 10,
})

# Plain-text source labels (no emoji — see module docstring)
_SOURCE_LABEL = {
    "voice": "voice",
    "text": "text",
    "telegram": "telegram",
    "checkin": "check-in",
    "reminder": "reminder",
    "web": "web",
    "file": "file",
}


# --- helpers ---------------------------------------------------------------


async def _fetch_rows(db, sql: str, params=()) -> list[dict]:
    cur = await db.db.execute(sql, params)
    return [dict(r) for r in await cur.fetchall()]


def _date_axis(ax):
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=8))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    ax.tick_params(axis="x", rotation=0, labelsize=9)
    ax.grid(True, linestyle=":", alpha=0.6)


def _empty_panel(ax, title: str):
    ax.set_title(title, loc="left", fontsize=11, fontweight="bold")
    ax.text(0.5, 0.5, "нет данных за этот период",
            ha="center", va="center", color="#64748b",
            transform=ax.transAxes, fontsize=10)
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_visible(False)


# --- panels ----------------------------------------------------------------


async def _panel_notes_volume(db, ax, days: int):
    rows = await _fetch_rows(
        db,
        "SELECT date(created_at) AS d, COUNT(*) AS n FROM notes "
        "WHERE content!='' AND date(created_at) >= date('now','-' || ? || ' days') "
        "GROUP BY d ORDER BY d",
        (days,),
    )
    if not rows:
        _empty_panel(ax, "Активность заметок")
        return
    xs = [datetime.strptime(r["d"], "%Y-%m-%d") for r in rows]
    ys = [r["n"] for r in rows]
    ax.bar(xs, ys, color=_ACCENT, width=0.8, alpha=0.85)
    ax.set_title(
        f"Активность — {sum(ys)} заметок за {days} дн "
        f"(в среднем {sum(ys)/max(1,len(rows)):.1f}/день)",
        loc="left", fontsize=11, fontweight="bold")
    ax.set_ylabel("заметок/день")
    _date_axis(ax)


async def _panel_mood_energy(db, ax, days: int):
    rows = await _fetch_rows(
        db,
        "SELECT date(n.created_at) AS d, "
        "  AVG(CAST(ne.mood_score AS REAL)) AS mood, "
        "  AVG(CAST(ne.energy AS REAL)) AS energy "
        "FROM notes n JOIN note_enrichments ne ON ne.note_id = n.id "
        "WHERE date(n.created_at) >= date('now','-' || ? || ' days') "
        "  AND (ne.mood_score IS NOT NULL OR ne.energy IS NOT NULL) "
        "GROUP BY d ORDER BY d",
        (days,),
    )
    if not rows:
        _empty_panel(ax, "Настроение и энергия")
        return
    xs = [datetime.strptime(r["d"], "%Y-%m-%d") for r in rows]
    moods = [r["mood"] for r in rows]
    energies = [r["energy"] for r in rows]
    if any(m is not None for m in moods):
        ax.plot(xs, moods, color=_GOOD, marker="o", lw=2, label="настроение")
    if any(e is not None for e in energies):
        ax.plot(xs, energies, color=_PURPLE, marker="s", lw=2, label="энергия")
    ax.set_ylim(0, 11)
    ax.set_yticks([2, 4, 6, 8, 10])
    ax.set_title("Настроение и энергия (1-10)",
                 loc="left", fontsize=11, fontweight="bold")
    ax.legend(loc="lower left", facecolor=_BG, edgecolor=_GRID,
              fontsize=9, framealpha=0.9)
    _date_axis(ax)


async def _panel_sentiment(db, ax, days: int):
    rows = await _fetch_rows(
        db,
        "SELECT date(n.created_at) AS d, AVG(ne.sentiment) AS s "
        "FROM notes n JOIN note_enrichments ne ON ne.note_id = n.id "
        "WHERE date(n.created_at) >= date('now','-' || ? || ' days') "
        "  AND ne.sentiment IS NOT NULL "
        "GROUP BY d ORDER BY d",
        (days,),
    )
    if not rows:
        _empty_panel(ax, "Эмоциональный тон")
        return
    xs = [datetime.strptime(r["d"], "%Y-%m-%d") for r in rows]
    ys = [r["s"] for r in rows]
    colors = [_GOOD if y > 0.15 else _BAD if y < -0.15 else _WARN for y in ys]
    ax.bar(xs, ys, color=colors, alpha=0.85, width=0.8)
    ax.axhline(0, color=_GRID, lw=1)
    ax.set_ylim(-1, 1)
    ax.set_title("Эмоциональный тон (sentiment, -1 ↔ +1)",
                 loc="left", fontsize=11, fontweight="bold")
    _date_axis(ax)


async def _panel_categories(db, ax, days: int):
    rows = await _fetch_rows(
        db,
        "SELECT COALESCE(NULLIF(ne.category,''), n.category, 'other') AS c, "
        "       COUNT(*) AS n "
        "FROM notes n LEFT JOIN note_enrichments ne ON ne.note_id = n.id "
        "WHERE n.content!='' "
        "  AND date(n.created_at) >= date('now','-' || ? || ' days') "
        "GROUP BY c ORDER BY n DESC LIMIT 8",
        (days,),
    )
    if not rows:
        _empty_panel(ax, "Топ-категории заметок")
        return
    labels = [r["c"] or "—" for r in rows]
    counts = [r["n"] for r in rows]
    bars = ax.barh(range(len(labels)), counts, color=_ACCENT, alpha=0.85)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_title("Топ-категории заметок",
                 loc="left", fontsize=11, fontweight="bold")
    for b, c in zip(bars, counts):
        ax.text(b.get_width() + 0.2, b.get_y() + b.get_height() / 2,
                str(c), va="center", color=_FG, fontsize=9)
    ax.tick_params(axis="x", labelsize=9)
    ax.grid(True, axis="x", linestyle=":", alpha=0.4)


async def _panel_sources(db, ax, days: int):
    rows = await _fetch_rows(
        db,
        "SELECT source, COUNT(*) AS n FROM notes "
        "WHERE content!='' "
        "  AND date(created_at) >= date('now','-' || ? || ' days') "
        "GROUP BY source ORDER BY n DESC",
        (days,),
    )
    if not rows:
        _empty_panel(ax, "Источники")
        return
    labels = [_SOURCE_LABEL.get(r["source"], r["source"] or "?") for r in rows]
    counts = [r["n"] for r in rows]
    palette = [_ACCENT, _PURPLE, _GOOD, _WARN, _BAD, "#fb7185", "#94a3b8"]
    ax.pie(counts, labels=labels, autopct="%1.0f%%",
           colors=palette[:len(counts)], startangle=90,
           textprops={"color": _FG, "fontsize": 9})
    ax.set_title("Источники заметок",
                 loc="left", fontsize=11, fontweight="bold")


async def _panel_files_by_category(db, ax):
    rows = await _fetch_rows(
        db,
        "SELECT category, COUNT(*) AS n FROM files "
        "GROUP BY category ORDER BY n DESC LIMIT 6",
    )
    if not rows:
        _empty_panel(ax, "Документы по категориям")
        return
    labels = [r["category"] or "—" for r in rows]
    counts = [r["n"] for r in rows]
    bars = ax.barh(range(len(labels)), counts, color=_PURPLE, alpha=0.85)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_title(f"Документы — {sum(counts)} всего",
                 loc="left", fontsize=11, fontweight="bold")
    for b, c in zip(bars, counts):
        ax.text(b.get_width() + 0.2, b.get_y() + b.get_height() / 2,
                str(c), va="center", color=_FG, fontsize=9)
    ax.tick_params(axis="x", labelsize=9)
    ax.grid(True, axis="x", linestyle=":", alpha=0.4)


# --- top-level composers ---------------------------------------------------


async def build_dashboard_png(db, days: int = 30) -> bytes:
    """6-panel dashboard. Returns PNG bytes."""
    fig = plt.figure(figsize=(12, 9), dpi=110)
    gs = fig.add_gridspec(3, 2, hspace=0.5, wspace=0.25,
                          left=0.06, right=0.97, top=0.93, bottom=0.07)
    fig.suptitle(
        f"Dashboard — последние {days} дн",
        color=_FG, fontsize=14, fontweight="bold", y=0.985,
    )

    await _panel_notes_volume(db, fig.add_subplot(gs[0, 0]), days)
    await _panel_mood_energy(db, fig.add_subplot(gs[0, 1]), days)
    await _panel_sentiment(db, fig.add_subplot(gs[1, 0]), days)
    await _panel_categories(db, fig.add_subplot(gs[1, 1]), days)
    await _panel_sources(db, fig.add_subplot(gs[2, 0]), days)
    await _panel_files_by_category(db, fig.add_subplot(gs[2, 1]))

    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


async def build_today_png(db) -> bytes:
    """Single-panel timeline of today's notes."""
    today = date.today().isoformat()
    rows = await _fetch_rows(
        db,
        "SELECT n.id, n.title, n.source, time(n.created_at) AS t, "
        "       COALESCE(ne.category, n.category) AS cat, "
        "       ne.mood_score AS mood, ne.energy AS energy "
        "FROM notes n LEFT JOIN note_enrichments ne ON ne.note_id = n.id "
        "WHERE date(n.created_at) = ? AND n.content != '' "
        "ORDER BY n.created_at",
        (today,),
    )

    fig = plt.figure(figsize=(10, 6), dpi=110)
    fig.suptitle(f"Сегодня — {today}", color=_FG, fontsize=14,
                 fontweight="bold", y=0.97)
    ax = fig.add_subplot(111)
    ax.set_xlim(0, 24)
    ax.set_ylim(0, 1)
    ax.set_xticks(range(0, 25, 3))
    ax.set_xticklabels([f"{h:02d}" for h in range(0, 25, 3)])
    ax.set_yticks([])
    ax.grid(True, axis="x", linestyle=":", alpha=0.4)
    ax.set_title(f"{len(rows)} заметок", loc="left", fontsize=11)

    if not rows:
        ax.text(12, 0.5, "тишина — ни одной заметки сегодня",
                ha="center", va="center", color="#64748b",
                fontsize=12)
    else:
        for i, r in enumerate(rows):
            t = r.get("t") or "00:00:00"
            try:
                hh, mm = int(t[:2]), int(t[3:5])
            except Exception:
                hh = mm = 0
            x = hh + mm / 60.0
            cat = r.get("cat") or ""
            label = (r.get("title") or "")[:50] or "—"
            color = _ACCENT
            if cat == "fitness":
                color = _PURPLE
            elif cat in ("food", "drink"):
                color = _WARN
            elif cat in ("symptom", "mood"):
                color = _BAD
            elif cat == "idea":
                color = _GOOD
            ax.scatter([x], [0.5], s=140, color=color, alpha=0.9,
                       edgecolors=_FG, linewidths=1)
            offset = 0.18 if i % 2 == 0 else -0.22
            ax.annotate(
                label,
                xy=(x, 0.5), xytext=(x, 0.5 + offset),
                fontsize=8, color=_FG, ha="center",
                arrowprops=dict(arrowstyle="-", color=_GRID, lw=0.8),
            )
    ax.set_xlabel("час дня", color=_FG, fontsize=9)

    buf = BytesIO()
    fig.savefig(buf, format="png", facecolor=_BG, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
