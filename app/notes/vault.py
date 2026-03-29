"""Obsidian Vault Manager — structured note storage with wikilinks and daily MOC."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class ObsidianVault:
    """Manage Obsidian-compatible markdown vault with category-based structure."""

    def __init__(self, base_path: str | Path, encryption_key: bytes | None = None):
        self.base_path = Path(base_path).expanduser().resolve()
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._enc_key = encryption_key

    def _write_file(self, path: Path, text: str):
        """Write text to file, encrypting if encryption key is set."""
        if self._enc_key:
            from app.utils.crypto import encrypt_bytes
            data = encrypt_bytes(text.encode("utf-8"), self._enc_key)
            path.write_bytes(data)
        else:
            path.write_text(text, encoding="utf-8")

    def _read_file(self, path: Path) -> str:
        """Read text from file, decrypting if encrypted."""
        if self._enc_key:
            from app.utils.crypto import is_encrypted, decrypt_bytes
            data = path.read_bytes()
            if is_encrypted(data):
                return decrypt_bytes(data, self._enc_key).decode("utf-8")
        return path.read_text(encoding="utf-8")

    def _ensure_dir(self, category: str, subcategory: str = "") -> Path:
        """Ensure category/subcategory directory exists."""
        path = self.base_path / category
        if subcategory:
            path = path / subcategory
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _slugify(self, text: str, max_len: int = 40) -> str:
        """Create a filesystem-safe slug from text."""
        slug = re.sub(r'[^\w\s-]', '', text.lower()).strip()
        slug = re.sub(r'[\s]+', '-', slug)
        return slug[:max_len]

    def write_note(
        self,
        note_id: int,
        category: str,
        subcategory: str,
        title: str,
        content: str,
        tags: list[str],
        summary: str = "",
        related_notes: list[dict] | None = None,
        structured_data: dict | None = None,
        mood_score: int | None = None,
        source: str = "voice",
        sentiment: float | None = None,
        energy: int | None = None,
        confidence: float = 0.0,
    ) -> Path:
        """Write a categorized note as Obsidian markdown."""
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        slug = self._slugify(title)
        filename = f"{date_str}_{slug}.md"

        # Low confidence → _inbox folder
        if category == "_inbox":
            note_dir = self._ensure_dir("_inbox")
        else:
            note_dir = self._ensure_dir(category, subcategory)
        md_path = note_dir / filename

        # Avoid overwrites — append number if exists
        counter = 1
        while md_path.exists():
            counter += 1
            md_path = note_dir / f"{date_str}_{slug}-{counter}.md"

        # Build YAML frontmatter
        tag_str = ", ".join(tags) if tags else ""
        lines = [
            "---",
            f"date: {date_str}",
            f"time: {now.strftime('%H:%M')}",
            f"category: {category}",
            f"subcategory: {subcategory}",
            f"source: {source}",
            f"tags: [{tag_str}]",
            f"note_id: {note_id}",
            f"confidence: {confidence:.2f}",
        ]
        if mood_score is not None:
            lines.append(f"mood: {mood_score}")
        if sentiment is not None:
            lines.append(f"sentiment: {sentiment:.2f}")
        if energy is not None:
            lines.append(f"energy: {energy}")

        # Add linked note IDs
        if related_notes:
            linked_ids = [str(rn.get("id", "")) for rn in related_notes if rn.get("id")]
            if linked_ids:
                lines.append(f"linked: [{', '.join(linked_ids)}]")

        lines.extend(["---", ""])

        # Title and summary
        lines.append(f"# {title}")
        lines.append("")
        if summary and summary != content:
            lines.append(summary)
            lines.append("")

        # Structured data section
        if structured_data:
            lines.append("## Данные")
            for key, value in structured_data.items():
                if isinstance(value, list):
                    lines.append(f"- **{key}**: {', '.join(str(v) for v in value)}")
                else:
                    lines.append(f"- **{key}**: {value}")
            lines.append("")

        # Original text
        lines.extend(["## Оригинал", "", content, ""])

        # Wikilinks to related notes
        if related_notes:
            lines.append("## Связи")
            for rn in related_notes[:10]:
                rn_title = rn.get("title", "")
                rn_cat = rn.get("category", "")
                rn_subcat = rn.get("subcategory", "")
                rn_vault = rn.get("vault_path", "")
                if rn_vault:
                    # Use relative path from vault root
                    try:
                        rel = Path(rn_vault).relative_to(self.base_path)
                        stem = rel.with_suffix("").as_posix()
                    except ValueError:
                        stem = Path(rn_vault).stem
                else:
                    stem = self._slugify(rn_title)
                lines.append(f"- [[{stem}]] — {rn_title}")
            lines.append("")

        # Daily MOC link
        lines.append(f"---\n[[daily/{date_str}]]")

        self._write_file(md_path, "\n".join(lines))
        logger.info(f"Vault note written: {md_path}")
        return md_path

    def update_daily_moc(self, date: str, notes: list[dict], metrics: dict | None = None) -> Path:
        """Create or update daily Map of Content."""
        daily_dir = self._ensure_dir("daily")
        moc_path = daily_dir / f"{date}.md"

        # Group notes by category
        by_category: dict[str, list[dict]] = {}
        for note in notes:
            cat = note.get("category", "uncategorized") or "uncategorized"
            by_category.setdefault(cat, []).append(note)

        # Build frontmatter
        lines = [
            "---",
            f"date: {date}",
            "type: daily_moc",
            f"notes_count: {len(notes)}",
        ]
        if metrics:
            for k, v in metrics.items():
                if v is not None:
                    lines.append(f"{k}: {v}")
        lines.extend(["---", "", f"# {date} — Дневной обзор", ""])

        # Summary stats
        if metrics:
            stats = []
            if "calories_total" in metrics and metrics["calories_total"]:
                stats.append(f"Калории: ~{int(metrics['calories_total'])} kcal")
            if "mood_avg" in metrics and metrics["mood_avg"]:
                stats.append(f"Настроение: {metrics['mood_avg']:.1f}/10")
            if "weight" in metrics and metrics["weight"]:
                stats.append(f"Вес: {metrics['weight']} кг")
            if stats:
                lines.append("> " + " | ".join(stats))
                lines.append("")

        # Category emoji mapping
        cat_emoji = {
            "food": "🍽", "health": "🏥", "fitness": "💪",
            "business": "💼", "personal": "💭", "finance": "💰",
            "learning": "📚", "goals": "🎯",
        }

        # Sections by category
        for cat, cat_notes in sorted(by_category.items()):
            emoji = cat_emoji.get(cat, "📝")
            lines.append(f"## {emoji} {cat.capitalize()} ({len(cat_notes)})")
            for note in cat_notes:
                title = note.get("title", "Без названия")
                vault_path = note.get("vault_path", "")
                if vault_path:
                    try:
                        rel = Path(vault_path).relative_to(self.base_path)
                        stem = rel.with_suffix("").as_posix()
                    except ValueError:
                        stem = self._slugify(title)
                else:
                    stem = self._slugify(title)
                summary = note.get("summary", "")
                if not summary:
                    content = note.get("content", "")
                    summary = content[:80] + "..." if len(content) > 80 else content
                lines.append(f"- [[{stem}]] {summary}")
            lines.append("")

        self._write_file(moc_path, "\n".join(lines))
        logger.info(f"Daily MOC updated: {moc_path}")
        return moc_path

    def update_category_moc(self, category: str, notes: list[dict]) -> Path:
        """Create/update a Map of Content for a specific category."""
        cat_dir = self._ensure_dir(category)
        moc_path = cat_dir / "_moc.md"

        cat_emoji = {
            "food": "🍽", "health": "🏥", "fitness": "💪",
            "business": "💼", "personal": "💭", "finance": "💰",
            "learning": "📚", "goals": "🎯", "people": "👥",
            "ideas": "💡", "family": "👨‍👩‍👧‍👦", "auto": "🚗",
        }
        emoji = cat_emoji.get(category, "📝")

        # Group by subcategory
        by_sub: dict[str, list[dict]] = {}
        all_tags: dict[str, int] = {}
        for n in notes:
            sub = n.get("subcategory", "") or "general"
            by_sub.setdefault(sub, []).append(n)
            tags = n.get("tags", "[]")
            if isinstance(tags, str):
                import json
                try:
                    tags = json.loads(tags)
                except Exception:
                    tags = []
            for t in tags:
                all_tags[t] = all_tags.get(t, 0) + 1

        lines = [
            "---",
            f"type: category_moc",
            f"category: {category}",
            f"notes_count: {len(notes)}",
            "---",
            "",
            f"# {emoji} {category.capitalize()} — Map of Content",
            "",
            f"Всего заметок: **{len(notes)}**",
            "",
        ]

        # Tag cloud
        if all_tags:
            top_tags = sorted(all_tags.items(), key=lambda x: x[1], reverse=True)[:15]
            tag_line = " ".join(f"`{t}` ({c})" for t, c in top_tags)
            lines.extend(["## Теги", tag_line, ""])

        # Sections by subcategory
        for sub, sub_notes in sorted(by_sub.items()):
            lines.append(f"## {sub} ({len(sub_notes)})")
            for n in sub_notes[:20]:  # limit per subcategory
                title = n.get("title", "Без названия")
                vault_path = n.get("vault_path", "")
                date = n.get("created_at", "")[:10]
                if vault_path:
                    try:
                        rel = Path(vault_path).relative_to(self.base_path)
                        stem = rel.with_suffix("").as_posix()
                    except ValueError:
                        stem = self._slugify(title)
                else:
                    stem = self._slugify(title)
                lines.append(f"- [[{stem}]] ({date}) {title}")
            if len(sub_notes) > 20:
                lines.append(f"- ...и ещё {len(sub_notes) - 20}")
            lines.append("")

        self._write_file(moc_path, "\n".join(lines))
        logger.info(f"Category MOC updated: {moc_path}")
        return moc_path

    def add_backlink(self, target_path: Path, source_stem: str) -> bool:
        """Add a backlink to an existing note if not already present."""
        if not target_path.exists():
            return False
        content = self._read_file(target_path)
        link = f"[[{source_stem}]]"
        if link in content:
            return False  # already linked

        # Add to Связи section or create one
        if "## Связи" in content:
            content = content.replace("## Связи\n", f"## Связи\n- {link}\n", 1)
        else:
            content += f"\n## Связи\n- {link}\n"

        self._write_file(target_path, content)
        return True
