"""Tests for skill engine."""

import pytest
from pathlib import Path

from app.skills.engine import SkillEngine, SkillDefinition


@pytest.fixture
def skills_dir(tmp_dir):
    d = tmp_dir / "skills"
    d.mkdir()
    # Create a test skill
    (d / "health.yaml").write_text("""
name: health
display_name: Health
description: Medical documents
category: health
priority: 10
enabled: true
routing_rules:
  keywords:
    - diagnosis
    - patient
    - blood test
    - medical
  patterns:
    - '\\b(WBC|RBC|HGB)\\b'
  min_confidence: 0.3
naming_template: "{date}_{document_type}"
extraction:
  fields:
    - name: document_type
      description: Type
      required: true
""")
    (d / "business.yaml").write_text("""
name: business
category: business
priority: 5
enabled: true
routing_rules:
  keywords:
    - invoice
    - payment
    - receipt
    - contract
  min_confidence: 0.3
""")
    return d


class TestSkillEngine:
    @pytest.mark.asyncio
    async def test_load_all(self, skills_dir):
        engine = SkillEngine(skills_dir)
        await engine.load_all()
        skills = engine.list_skills()
        assert len(skills) == 2

    @pytest.mark.asyncio
    async def test_get_skill(self, skills_dir):
        engine = SkillEngine(skills_dir)
        await engine.load_all()
        s = engine.get_skill("health")
        assert s is not None
        assert s.category == "health"
        assert s.priority == 10

    @pytest.mark.asyncio
    async def test_match_medical(self, skills_dir):
        engine = SkillEngine(skills_dir)
        await engine.load_all()
        skill, score = engine.match_skill("patient blood test results WBC normal")
        assert skill is not None
        assert skill.name == "health"
        assert score > 0.3

    @pytest.mark.asyncio
    async def test_match_business(self, skills_dir):
        engine = SkillEngine(skills_dir)
        await engine.load_all()
        skill, score = engine.match_skill("invoice for payment total amount receipt")
        assert skill is not None
        assert skill.name == "business"

    @pytest.mark.asyncio
    async def test_no_match(self, skills_dir):
        engine = SkillEngine(skills_dir)
        await engine.load_all()
        skill, score = engine.match_skill("random gibberish xyz abc")
        # Might match with low score or not at all
        if skill:
            assert score < 0.5

    @pytest.mark.asyncio
    async def test_get_categories(self, skills_dir):
        engine = SkillEngine(skills_dir)
        await engine.load_all()
        cats = engine.get_categories()
        assert "health" in cats
        assert "business" in cats

    @pytest.mark.asyncio
    async def test_priority_ordering(self, skills_dir):
        engine = SkillEngine(skills_dir)
        await engine.load_all()
        skills = engine.list_skills()
        assert skills[0].name == "health"  # priority 10 > 5

    @pytest.mark.asyncio
    async def test_hot_reload(self, skills_dir):
        engine = SkillEngine(skills_dir)
        await engine.load_all()
        # Modify a skill
        import time
        time.sleep(0.1)
        (skills_dir / "health.yaml").write_text("""
name: health
category: health_updated
priority: 20
enabled: true
routing_rules:
  keywords: [updated]
  min_confidence: 0.3
""")
        changed = await engine.reload_changed()
        assert "health" in changed
        s = engine.get_skill("health")
        assert s.category == "health_updated"
