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
