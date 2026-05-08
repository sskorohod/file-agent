"""Dev memory: per-project ingestion of code repos and decisions.

Each registered project gets its own cognee dataset, ``dev_<id>``. Codex,
Claude Code, and the like read from these via the cognee-mcp surface
(Phase 6) or directly from the FAG HTTP API.

This module owns three operations:

* ``register_project`` — book-keeping in SQLite. The cognee dataset is
  created lazily on the first ingest, not here.
* ``ingest_repo`` — walk a git working tree (or an arbitrary directory),
  pick out source files we want indexed, and push them to cognee.
* ``ingest_text`` — single free-form text record (decisions, design notes,
  bug postmortems, session logs).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.memory.cognee_client import CogneeClient, CogneeError

logger = logging.getLogger(__name__)


def _project_email(project_id: int) -> str:
    """Derive the cognee account email for a given project_id."""
    return f"dev-{project_id}@example.com"

# Extensions cognee should index. Code, configs, docs.
_INGEST_EXTENSIONS = frozenset({
    ".md", ".mdx", ".rst", ".txt",
    ".py", ".pyi", ".ipynb",
    ".ts", ".tsx", ".js", ".jsx", ".mjs",
    ".go", ".rs", ".java", ".kt", ".swift",
    ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".rb", ".php", ".scala",
    ".sh", ".bash", ".zsh",
    ".sql", ".graphql", ".proto",
    ".yaml", ".yml", ".toml", ".json",
})

# Extension-less files we still want by exact basename.
_INGEST_BASENAMES = frozenset({
    "README", "LICENSE", "CHANGELOG", "AUTHORS",
    "Makefile", "Dockerfile", "Procfile",
    "CLAUDE.md", "AGENTS.md", "GEMINI.md",
})

# Hard cap on files per ingest_repo call. Enforced before we touch cognee.
MAX_FILES_PER_INGEST = 1000

# Files larger than this are likely vendored/built artifacts; skip.
MAX_FILE_BYTES = 256 * 1024  # 256 KB


@dataclass
class IngestRepoResult:
    project_id: int
    dataset: str
    discovered: int
    ingested: int
    skipped_too_large: int
    skipped_unreadable: int
    over_limit: bool
    error: str | None = None


class DevIngestor:
    """Send repository contents and decisions into per-project cognee datasets."""

    def __init__(self, db, cognee_client: CogneeClient):
        self.db = db
        self.cognee = cognee_client

    @staticmethod
    def dataset_name(project_id: int) -> str:
        return f"dev_{project_id}"

    async def register_project(
        self, name: str, repo_path: str = "", description: str = "",
    ) -> dict:
        """Create a project row + cognee user for it.

        Idempotent on (name). On first creation, registers a fresh cognee
        account ``dev-<id>@example.com`` whose JWT bearer is stored on the
        project row. All later ingest/recall for this project use that
        token, so ACL keeps the project's data isolated from ``personal``
        and from other projects.
        """
        existing = await self.db.get_dev_project_by_name(name)
        if existing:
            return existing

        project_id = await self.db.create_dev_project(name, repo_path, description)

        # Provision the per-project cognee user. Failure here leaves the
        # SQLite row in place with empty credentials — ingest will refuse
        # to run until the credentials are filled in. We do not roll back
        # the SQLite row because the user can re-attempt registration via
        # a maintenance command later.
        if self.cognee.healthy:
            email = _project_email(project_id)
            password = secrets.token_urlsafe(32)
            try:
                token = await self.cognee.register_and_login(email, password)
                await self.db.update_dev_project_cognee_creds(
                    project_id, email=email, token=token,
                )
                logger.info("dev project %s: cognee user %s ready", project_id, email)
            except CogneeError as exc:
                logger.warning(
                    "dev project %s: cognee user provisioning failed: %s",
                    project_id, exc,
                )
        else:
            logger.warning(
                "dev project %s registered but sidecar is unhealthy — no cognee user provisioned",
                project_id,
            )

        return await self.db.get_dev_project(project_id)

    async def ingest_repo(
        self, project_id: int, repo_path: str | None = None,
    ) -> IngestRepoResult:
        """Walk a repo and ingest source files into ``dev_<id>``.

        If ``repo_path`` is omitted, we use the path stored on the project.
        Git-tracked files are preferred (.gitignore is respected); for
        non-git directories we fall back to a filtered rglob.
        """
        project = await self.db.get_dev_project(project_id)
        if not project:
            return IngestRepoResult(
                project_id=project_id, dataset="", discovered=0,
                ingested=0, skipped_too_large=0, skipped_unreadable=0,
                over_limit=False, error="project not found",
            )

        path_str = repo_path or project.get("repo_path", "")
        if not path_str:
            return IngestRepoResult(
                project_id=project_id, dataset=self.dataset_name(project_id),
                discovered=0, ingested=0, skipped_too_large=0,
                skipped_unreadable=0, over_limit=False,
                error="no repo_path provided",
            )

        root = Path(path_str).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            return IngestRepoResult(
                project_id=project_id, dataset=self.dataset_name(project_id),
                discovered=0, ingested=0, skipped_too_large=0,
                skipped_unreadable=0, over_limit=False,
                error=f"repo_path not a directory: {root}",
            )

        candidates = self._discover_files(root)
        result = IngestRepoResult(
            project_id=project_id,
            dataset=self.dataset_name(project_id),
            discovered=len(candidates),
            ingested=0,
            skipped_too_large=0,
            skipped_unreadable=0,
            over_limit=len(candidates) > MAX_FILES_PER_INGEST,
        )
        if result.over_limit:
            result.error = (
                f"too many files: {len(candidates)} > limit {MAX_FILES_PER_INGEST}"
            )
            return result

        if not self.cognee.healthy:
            result.error = "cognee sidecar unhealthy"
            return result

        token = project.get("cognee_token") or ""
        if not token:
            result.error = "project has no cognee user (re-register the project)"
            return result

        dataset = result.dataset
        for path in candidates:
            try:
                size = path.stat().st_size
            except OSError:
                result.skipped_unreadable += 1
                continue
            if size > MAX_FILE_BYTES:
                result.skipped_too_large += 1
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                result.skipped_unreadable += 1
                continue
            if not content.strip():
                result.skipped_unreadable += 1
                continue

            rel = path.relative_to(root)
            try:
                await self.cognee.add(
                    content=content,
                    dataset=dataset,
                    filename=str(rel),
                    token=token,
                )
                result.ingested += 1
            except CogneeError as exc:
                logger.warning(
                    "cognee add failed for %s in %s: %s",
                    rel, dataset, exc,
                )
                result.skipped_unreadable += 1

        # One cognify pass at the end is much cheaper than per-file.
        if result.ingested > 0:
            try:
                await self.cognee.cognify(dataset=dataset, token=token)
            except CogneeError as exc:
                logger.warning("cognify failed for %s: %s", dataset, exc)
                result.error = f"cognify failed: {exc}"

        return result

    async def ingest_text(
        self,
        project_id: int,
        *,
        content: str,
        source_type: str = "decision",
        source_id: str = "",
    ) -> bool:
        """Send a single free-form text record (decision, postmortem, session log).

        Returns True only if cognee accepted both add and cognify.
        """
        project = await self.db.get_dev_project(project_id)
        if not project:
            return False
        if not self.cognee.healthy:
            return False
        if not content or len(content) < 40:
            return False

        token = project.get("cognee_token") or ""
        if not token:
            logger.warning(
                "dev project %s has no cognee user — re-register the project",
                project_id,
            )
            return False

        dataset = self.dataset_name(project_id)
        upload_name = f"{source_type}_{source_id or 'note'}.txt"
        try:
            await self.cognee.add(
                content=content, dataset=dataset, filename=upload_name, token=token,
            )
            await self.cognee.cognify(dataset=dataset, token=token)
        except CogneeError as exc:
            logger.warning(
                "dev ingest_text failed for project %s/%s: %s",
                project_id, source_type, exc,
            )
            return False
        return True

    # ── discovery ───────────────────────────────────────────────────────

    def _discover_files(self, root: Path) -> list[Path]:
        """Find source files in ``root`` honouring .gitignore when possible."""
        git_files = self._git_ls_files(root)
        if git_files is not None:
            paths = [root / f for f in git_files]
        else:
            paths = list(root.rglob("*"))

        keep: list[Path] = []
        for p in paths:
            if not p.is_file():
                continue
            if any(part.startswith(".") and part not in (".github",) for part in p.relative_to(root).parts):
                # Hidden directories (.git, .venv, .pytest_cache) — skip.
                continue
            if p.suffix.lower() in _INGEST_EXTENSIONS:
                keep.append(p)
                continue
            stem = p.name.split(".", 1)[0]
            if p.name in _INGEST_BASENAMES or stem in _INGEST_BASENAMES:
                keep.append(p)
        keep.sort()
        return keep

    @staticmethod
    def _git_ls_files(root: Path) -> list[str] | None:
        """Return tracked files via ``git ls-files``, or None if not a git repo."""
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "ls-files", "-z"],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return [f for f in proc.stdout.decode("utf-8", errors="replace").split("\x00") if f]
