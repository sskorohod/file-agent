"""Inbox Watcher — monitors a folder for new .md files and processes them."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class InboxWatcher:
    """Watch an inbox folder for new markdown files and capture them."""

    def __init__(self, inbox_path: str | Path, archive_path: str | Path, db, capture):
        self.inbox_path = Path(inbox_path).expanduser().resolve()
        self.archive_path = Path(archive_path).expanduser().resolve()
        self.db = db
        self._capture = capture
        self._processed: set[str] = set()

        self.inbox_path.mkdir(parents=True, exist_ok=True)
        self.archive_path.mkdir(parents=True, exist_ok=True)

    async def scan_once(self) -> int:
        """Scan inbox for unprocessed .md files. Returns count of processed files."""
        count = 0
        for md_file in sorted(self.inbox_path.glob("*.md")):
            if md_file.name in self._processed:
                continue
            if md_file.name.startswith("."):
                continue

            try:
                # Wait for file to be fully written
                await asyncio.sleep(1)

                content = md_file.read_text(encoding="utf-8").strip()
                if not content:
                    continue

                # Strip YAML frontmatter if present (from FAG)
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        content = parts[2].strip()

                # Capture via service (enqueues enrichment)
                if self._capture:
                    note_id = await self._capture.capture(
                        content, source="file", title=md_file.stem,
                        content_type="markdown",
                    )
                else:
                    note_id = await self.db.save_note(
                        content=content, source="file", title=md_file.stem,
                    )

                # Archive original
                dest = self.archive_path / md_file.name
                counter = 1
                while dest.exists():
                    counter += 1
                    dest = self.archive_path / f"{md_file.stem}-{counter}{md_file.suffix}"
                shutil.move(str(md_file), str(dest))

                self._processed.add(md_file.name)
                count += 1
                logger.info(f"Inbox: processed {md_file.name} → note {note_id}")

            except Exception as e:
                logger.error(f"Inbox: failed to process {md_file.name}: {e}")
                self._processed.add(md_file.name)  # skip on next scan

        return count

    async def watch_loop(self, interval: int = 10):
        """Continuously watch inbox folder."""
        logger.info(f"Inbox watcher started: {self.inbox_path}")
        while True:
            try:
                processed = await self.scan_once()
                if processed:
                    logger.info(f"Inbox watcher: processed {processed} files")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Inbox watcher error: {e}")
            await asyncio.sleep(interval)
