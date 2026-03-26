"""Chart generation with matplotlib — dark theme matching the web UI."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

# Dark theme colors matching layout.html CSS vars
COLORS = {
    "bg": "#0d1117",
    "surface": "#161b22",
    "border": "#30363d",
    "text": "#e6edf3",
    "text2": "#8b949e",
    "accent": "#7c6bf0",
    "green": "#3fb950",
    "blue": "#58a6ff",
    "orange": "#d29922",
    "red": "#f85149",
}

LINE_COLORS = [
    COLORS["accent"],
    COLORS["green"],
    COLORS["blue"],
    COLORS["orange"],
    COLORS["red"],
    "#a5a0f5",
    "#79c0ff",
    "#e3b341",
]


def generate_time_series_chart(
    data_points: list[dict],
    title: str = "",
) -> bytes | None:
    """Generate a dark-themed PNG line chart from data points.

    Each data point: {date, metric, value, unit, reference_min, reference_max}.
    Groups by metric name, one line per metric.
    Returns PNG bytes or None if no data.
    """
    if not data_points:
        return None

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        logger.error("matplotlib not installed — cannot generate charts")
        return None

    # Group by metric
    metrics: dict[str, list[tuple[datetime, float]]] = {}
    ref_ranges: dict[str, tuple[float | None, float | None]] = {}
    units: dict[str, str] = {}

    for dp in data_points:
        name = dp.get("metric", "value")
        try:
            dt = datetime.fromisoformat(dp["date"])
            val = float(dp["value"])
        except (ValueError, KeyError, TypeError):
            continue

        metrics.setdefault(name, []).append((dt, val))
        units.setdefault(name, dp.get("unit", ""))

        ref_min = dp.get("reference_min")
        ref_max = dp.get("reference_max")
        if ref_min is not None or ref_max is not None:
            ref_ranges[name] = (
                float(ref_min) if ref_min is not None else None,
                float(ref_max) if ref_max is not None else None,
            )

    if not metrics:
        return None

    # Sort each series by date
    for name in metrics:
        metrics[name].sort(key=lambda x: x[0])

    # Determine layout: separate subplot per metric if units differ, else shared
    unique_units = set(units.values())
    use_subplots = len(metrics) > 1 and len(unique_units) > 1

    if use_subplots:
        n = len(metrics)
        fig, axes = plt.subplots(n, 1, figsize=(10, 3.5 * n), sharex=True)
        if n == 1:
            axes = [axes]
    else:
        fig, ax = plt.subplots(figsize=(10, 5))
        axes = [ax] * len(metrics)

    # Apply dark theme
    fig.patch.set_facecolor(COLORS["bg"])

    for i, (name, series) in enumerate(metrics.items()):
        ax = axes[i] if use_subplots else axes[0]
        ax.set_facecolor(COLORS["surface"])

        dates = [s[0] for s in series]
        values = [s[1] for s in series]
        color = LINE_COLORS[i % len(LINE_COLORS)]

        ax.plot(dates, values, color=color, linewidth=2, marker="o",
                markersize=5, label=name, zorder=3)

        # Reference range shading
        if name in ref_ranges:
            rmin, rmax = ref_ranges[name]
            if rmin is not None and rmax is not None:
                ax.axhspan(rmin, rmax, alpha=0.1, color=COLORS["green"],
                           label="Норма", zorder=1)
            elif rmin is not None:
                ax.axhline(rmin, color=COLORS["green"], linestyle="--",
                           alpha=0.5, linewidth=1)
            elif rmax is not None:
                ax.axhline(rmax, color=COLORS["green"], linestyle="--",
                           alpha=0.5, linewidth=1)

        # Value labels on points
        for dt, val in series:
            ax.annotate(f"{val:.1f}", (dt, val), textcoords="offset points",
                        xytext=(0, 8), ha="center", fontsize=8,
                        color=COLORS["text2"])

        # Styling
        unit = units.get(name, "")
        ylabel = f"{name}" + (f" ({unit})" if unit else "")
        ax.set_ylabel(ylabel, color=COLORS["text"], fontsize=11)
        ax.tick_params(colors=COLORS["text2"], labelsize=9)
        ax.grid(True, color=COLORS["border"], alpha=0.5, linewidth=0.5)

        for spine in ax.spines.values():
            spine.set_color(COLORS["border"])

        if use_subplots:
            ax.legend(loc="upper left", fontsize=9, facecolor=COLORS["surface"],
                      edgecolor=COLORS["border"], labelcolor=COLORS["text"])

    # Shared legend for single-axis chart
    if not use_subplots and len(metrics) > 1:
        axes[0].legend(loc="upper left", fontsize=9, facecolor=COLORS["surface"],
                       edgecolor=COLORS["border"], labelcolor=COLORS["text"])

    # X-axis date formatting on bottom axis
    bottom_ax = axes[-1] if use_subplots else axes[0]
    bottom_ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m.%Y"))
    bottom_ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    fig.autofmt_xdate(rotation=30)
    bottom_ax.tick_params(axis="x", colors=COLORS["text2"], labelsize=9)

    if title:
        fig.suptitle(title, color=COLORS["text"], fontsize=14, fontweight="bold",
                     y=0.98)

    fig.tight_layout(rect=[0, 0, 1, 0.95] if title else None)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, facecolor=COLORS["bg"],
                bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    buf.seek(0)
    return buf.read()
