"""Skill Engine — load, validate, match and hot-reload YAML skills."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Skill Schema ────────────────────────────────────────────────────────────

class RoutingRule(BaseModel):
    """Single routing rule — keyword or regex pattern."""
    keywords: list[str] = []
    patterns: list[str] = []  # regex patterns
    mime_types: list[str] = []
    min_confidence: float = 0.3


class ExtractionField(BaseModel):
    """Field to extract from document."""
    name: str
    description: str = ""
    required: bool = False


class ExtractionConfig(BaseModel):
    """What to extract from matched documents."""
    fields: list[ExtractionField] = []
    custom_prompt: str = ""


class SkillDefinition(BaseModel):
    """Full skill definition — loaded from YAML."""
    name: str
    display_name: str = ""
    description: str = ""
    category: str  # target category folder name
    storage_path: str = ""  # override subfolder under category
    routing_rules: RoutingRule = RoutingRule()
    naming_template: str = ""  # e.g. "{date}_{document_type}_{source}"
    extraction: ExtractionConfig = ExtractionConfig()
    response_template: str = ""  # Template for Telegram response, e.g. "📋 {document_type}\n📝 {summary}"
    tags: list[str] = []
    enabled: bool = True
    priority: int = 0  # higher = checked first
    # When true, every document matched by this skill is treated as
    # `sensitive` regardless of the LLM's per-document signal —
    # encrypted on disk, opening requires PIN.
    encrypt: bool = False

    @property
    def effective_display_name(self) -> str:
        return self.display_name or self.name.replace("_", " ").title()


# ── Skill Engine ────────────────────────────────────────────────────────────

class SkillEngine:
    """Manages skill lifecycle: load, validate, match, reload."""

    def __init__(self, skills_dir: str | Path):
        self.skills_dir = Path(skills_dir).resolve()
        self._skills: dict[str, SkillDefinition] = {}
        self._file_mtimes: dict[str, float] = {}

    async def load_all(self):
        """Load all YAML skills from directory."""
        if not self.skills_dir.exists():
            logger.warning(f"Skills directory not found: {self.skills_dir}")
            return

        count = 0
        for yaml_file in sorted(self.skills_dir.glob("*.yaml")):
            if yaml_file.name.startswith("_") or yaml_file.name == "TEMPLATE.yaml":
                continue
            try:
                skill = self._load_file(yaml_file)
                if skill:
                    self._skills[skill.name] = skill
                    self._file_mtimes[str(yaml_file)] = yaml_file.stat().st_mtime
                    count += 1
            except Exception as e:
                logger.error(f"Failed to load skill {yaml_file.name}: {e}")

        logger.info(f"Loaded {count} skills from {self.skills_dir}")

    def _load_file(self, path: Path) -> SkillDefinition | None:
        """Load and validate a single YAML skill file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        if not data or not isinstance(data, dict):
            return None
        # Use filename (without extension) as default name
        if "name" not in data:
            data["name"] = path.stem
        return SkillDefinition(**data)

    async def reload_changed(self) -> list[str]:
        """Hot-reload skills that have changed on disk."""
        changed = []
        for yaml_file in self.skills_dir.glob("*.yaml"):
            if yaml_file.name.startswith("_") or yaml_file.name == "TEMPLATE.yaml":
                continue
            key = str(yaml_file)
            current_mtime = yaml_file.stat().st_mtime
            if key not in self._file_mtimes or self._file_mtimes[key] < current_mtime:
                try:
                    skill = self._load_file(yaml_file)
                    if skill:
                        self._skills[skill.name] = skill
                        self._file_mtimes[key] = current_mtime
                        changed.append(skill.name)
                        logger.info(f"Reloaded skill: {skill.name}")
                except Exception as e:
                    logger.error(f"Failed to reload {yaml_file.name}: {e}")

        # Remove skills for deleted files
        existing_files = {str(f) for f in self.skills_dir.glob("*.yaml")}
        for key in list(self._file_mtimes):
            if key not in existing_files:
                del self._file_mtimes[key]
                # Find and remove the skill
                for name, skill in list(self._skills.items()):
                    if key.endswith(f"{name}.yaml"):
                        del self._skills[name]
                        changed.append(f"-{name}")

        return changed

    def list_skills(self) -> list[SkillDefinition]:
        """Return all loaded skills sorted by priority."""
        return sorted(
            [s for s in self._skills.values() if s.enabled],
            key=lambda s: s.priority,
            reverse=True,
        )

    def get_skill(self, name: str) -> SkillDefinition | None:
        return self._skills.get(name)

    def get_categories(self) -> list[str]:
        """Return unique categories from all skills."""
        return list(set(s.category for s in self._skills.values() if s.enabled))

    def match_skill(self, text: str, mime_type: str = "") -> tuple[SkillDefinition | None, float]:
        """Find the best matching skill for given text/mime. Returns (skill, confidence)."""
        text_lower = text.lower()
        best_skill = None
        best_score = 0.0

        for skill in self.list_skills():
            score = self._score_match(skill, text_lower, mime_type)
            if score > best_score and score >= skill.routing_rules.min_confidence:
                best_score = score
                best_skill = skill

        return best_skill, best_score

    def _score_match(self, skill: SkillDefinition, text_lower: str, mime_type: str) -> float:
        """Score how well text matches a skill's routing rules."""
        rules = skill.routing_rules
        score = 0.0
        matches = 0
        total_rules = 0

        # Keyword matching
        if rules.keywords:
            total_rules += 1
            keyword_hits = sum(1 for kw in rules.keywords if kw.lower() in text_lower)
            if keyword_hits > 0:
                score += keyword_hits / len(rules.keywords)
                matches += 1

        # Regex pattern matching
        if rules.patterns:
            total_rules += 1
            pattern_hits = 0
            for pattern in rules.patterns:
                try:
                    if re.search(pattern, text_lower, re.IGNORECASE):
                        pattern_hits += 1
                except re.error:
                    pass
            if pattern_hits > 0:
                score += pattern_hits / len(rules.patterns)
                matches += 1

        # MIME type matching
        if rules.mime_types and mime_type:
            total_rules += 1
            if mime_type in rules.mime_types:
                score += 1.0
                matches += 1

        if total_rules == 0:
            return 0.0
        return score / total_rules

    async def save_skill(self, skill: SkillDefinition) -> Path:
        """Save skill to YAML file."""
        path = self.skills_dir / f"{skill.name}.yaml"
        data = skill.model_dump(exclude_defaults=False)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        self._skills[skill.name] = skill
        self._file_mtimes[str(path)] = path.stat().st_mtime
        return path

    async def delete_skill(self, name: str) -> bool:
        """Delete a skill file."""
        path = self.skills_dir / f"{name}.yaml"
        if path.exists():
            path.unlink()
            self._skills.pop(name, None)
            self._file_mtimes.pop(str(path), None)
            return True
        return False
