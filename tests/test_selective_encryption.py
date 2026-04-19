"""Tests for selective encryption by skill."""

from pathlib import Path

import pytest

from app.skills.engine import SkillDefinition


class TestSkillEncryptField:
    def test_encrypt_default_is_false(self):
        skill = SkillDefinition(name="demo", category="demo")
        assert skill.encrypt is False

    def test_encrypt_true_parses(self):
        skill = SkillDefinition(name="demo", category="demo", encrypt=True)
        assert skill.encrypt is True

    def test_encrypt_false_parses(self):
        skill = SkillDefinition(name="demo", category="demo", encrypt=False)
        assert skill.encrypt is False


class TestSkillYamlEncryptFlag:
    @pytest.mark.asyncio
    async def test_personal_skill_has_encrypt_true(self):
        from app.skills.engine import SkillEngine
        engine = SkillEngine(Path(__file__).parent.parent / "skills")
        await engine.load_all()
        personal = engine.get_skill("personal")
        assert personal is not None, "personal skill should load from skills/"
        assert personal.encrypt is True

    @pytest.mark.asyncio
    async def test_business_skill_encrypt_default_false(self):
        from app.skills.engine import SkillEngine
        engine = SkillEngine(Path(__file__).parent.parent / "skills")
        await engine.load_all()
        business = engine.get_skill("business")
        assert business is not None
        assert business.encrypt is False
