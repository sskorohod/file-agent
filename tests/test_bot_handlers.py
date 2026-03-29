"""Tests for Telegram bot handlers — text/note choice, voice choice, search, webhook."""

from __future__ import annotations

import hashlib
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_update(text="", user_id=169108358, chat_type="private", chat_id=12345):
    """Build a minimal mock Update for testing handlers."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_chat.send_action = AsyncMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.chat_id = chat_id
    update.message.chat = MagicMock()
    update.message.chat.id = chat_id
    update.message.reply_text = AsyncMock(return_value=MagicMock(
        edit_text=AsyncMock(),
        message_id=1,
        chat=MagicMock(id=chat_id),
    ))
    update.callback_query = None
    return update


def _make_callback_update(data="", user_id=169108358, chat_id=12345):
    """Build a mock Update with callback_query for inline button tests."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    update.message = None
    update.callback_query = MagicMock()
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.edit_message_text = AsyncMock()
    update.callback_query.message = MagicMock()
    update.callback_query.message.chat_id = chat_id
    update.callback_query.message.chat = MagicMock()
    update.callback_query.message.chat.id = chat_id
    return update


def _make_context(bot=None):
    """Build a mock context."""
    ctx = MagicMock()
    ctx.args = []
    ctx.user_data = {}
    ctx.bot_data = {}
    ctx.bot = bot or MagicMock()
    ctx.bot.send_message = AsyncMock(return_value=MagicMock(
        message_id=2,
        chat=MagicMock(id=12345),
    ))
    return ctx


@pytest.fixture(autouse=True)
def reset_rate_limit():
    """Reset rate limiter between tests."""
    import app.bot.handlers as _h
    _h._last_command.clear()


@pytest_asyncio.fixture
async def bot_handlers(tmp_dir):
    """Create BotHandlers with mocked pipeline."""
    from app.storage.db import Database

    db = Database(tmp_dir / "test.db")
    await db.connect()

    pipeline = MagicMock()
    pipeline.db = db
    pipeline.llm = MagicMock()
    pipeline.llm.extract = AsyncMock(return_value=MagicMock(
        text='{"title": "Тестовая заметка", "summary": "Тест", "tags": ["тест"], "action_items": []}'
    ))
    pipeline.vector_store = MagicMock()
    pipeline.vector_store.search = AsyncMock(return_value=[])

    search_fn = AsyncMock(return_value={
        "text": "Найден документ X",
        "file_ids": {"abc123": "test.pdf"},
        "cached": False,
    })

    from app.bot.handlers import BotHandlers
    handlers = BotHandlers(pipeline, search_fn=search_fn)

    yield handlers

    await db.close()


# ── Text Handler (button choice) ────────────────────────────────────────

class TestHandleText:
    @pytest.mark.asyncio
    async def test_text_shows_two_buttons(self, bot_handlers):
        """Text message should show search and note buttons."""
        update = _make_update(text="Привет мир")
        ctx = _make_context()

        with patch("app.bot.handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
            )
            await bot_handlers.handle_text(update, ctx)

        update.message.reply_text.assert_called_once()
        call_args = update.message.reply_text.call_args
        assert "Привет мир" in call_args[0][0]
        # Check that reply_markup has inline keyboard
        reply_markup = call_args[1]["reply_markup"]
        buttons = reply_markup.inline_keyboard[0]
        assert len(buttons) == 2
        assert "Вопрос" in buttons[0].text
        assert "Заметка" in buttons[1].text

    @pytest.mark.asyncio
    async def test_text_stores_pending(self, bot_handlers):
        """Text should be stored in _pending_files for callback retrieval."""
        update = _make_update(text="тестовый текст")
        ctx = _make_context()

        with patch("app.bot.handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
            )
            await bot_handlers.handle_text(update, ctx)

        text_key = hashlib.md5("тестовый текст".encode()).hexdigest()[:8]
        assert f"tc:{text_key}" in bot_handlers._pending_files
        assert bot_handlers._pending_files[f"tc:{text_key}"] == "тестовый текст"

    @pytest.mark.asyncio
    async def test_empty_text_ignored(self, bot_handlers):
        """Empty text should be ignored."""
        update = _make_update(text="   ")
        ctx = _make_context()

        with patch("app.bot.handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
            )
            await bot_handlers.handle_text(update, ctx)

        update.message.reply_text.assert_not_called()


# ── Text Choice Callback ────────────────────────────────────────────────

class TestHandleTextChoice:
    @pytest.mark.asyncio
    async def test_search_choice(self, bot_handlers):
        """Choosing 'search' should trigger _do_search."""
        text_key = hashlib.md5("найди мой паспорт".encode()).hexdigest()[:8]
        bot_handlers._pending_files[f"tc:{text_key}"] = "найди мой паспорт"

        update = _make_callback_update(data=f"tc:s:{text_key}")
        ctx = _make_context()

        with patch("app.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
            )
            with patch.object(bot_handlers, "_do_search", new_callable=AsyncMock) as mock_search:
                await bot_handlers.handle_text_choice(update, ctx)

        # Should have called _do_search with the text
        mock_search.assert_called_once()
        assert mock_search.call_args[0][1] == "найди мой паспорт"
        # Pending should be consumed
        assert f"tc:{text_key}" not in bot_handlers._pending_files

    @pytest.mark.asyncio
    async def test_note_choice(self, bot_handlers, tmp_dir):
        """Choosing 'note' should save a note."""
        text_key = hashlib.md5("купить молоко".encode()).hexdigest()[:8]
        bot_handlers._pending_files[f"tc:{text_key}"] = "купить молоко"

        update = _make_callback_update(data=f"tc:n:{text_key}")
        ctx = _make_context()

        with patch("app.config.get_settings") as mock_settings:
            storage_mock = MagicMock()
            storage_mock.resolved_path = tmp_dir
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
                storage=storage_mock,
            )
            await bot_handlers.handle_text_choice(update, ctx)

        # Note should be saved in DB
        notes = await bot_handlers.pipeline.db.list_notes()
        assert len(notes) >= 1

        # Pending consumed
        assert f"tc:{text_key}" not in bot_handlers._pending_files

    @pytest.mark.asyncio
    async def test_expired_data(self, bot_handlers):
        """Expired text_key should show error message."""
        update = _make_callback_update(data="tc:s:deadbeef")
        ctx = _make_context()

        with patch("app.bot.handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
            )
            await bot_handlers.handle_text_choice(update, ctx)

        update.callback_query.edit_message_text.assert_called_with(
            "❌ Данные устарели. Отправьте сообщение заново."
        )


# ── Voice Choice Callback ───────────────────────────────────────────────

class TestHandleVoiceChoice:
    @pytest.mark.asyncio
    async def test_voice_search(self, bot_handlers):
        """Voice 'search' choice should trigger search."""
        bot_handlers._pending_files["vc:abcd1234"] = "где мои документы"

        update = _make_callback_update(data="vc:s:abcd1234")
        ctx = _make_context()

        with patch("app.bot.handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
            )
            await bot_handlers.handle_voice_choice(update, ctx)

        bot_handlers.search_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_voice_note(self, bot_handlers, tmp_dir):
        """Voice 'note' choice should save as note with source=voice."""
        bot_handlers._pending_files["vc:abcd1234"] = "заметка из голоса"

        update = _make_callback_update(data="vc:n:abcd1234")
        ctx = _make_context()

        with patch("app.config.get_settings") as mock_settings:
            storage_mock = MagicMock()
            storage_mock.resolved_path = tmp_dir
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
                storage=storage_mock,
            )
            await bot_handlers.handle_voice_choice(update, ctx)

        notes = await bot_handlers.pipeline.db.list_notes()
        assert len(notes) >= 1
        assert notes[0]["source"] == "voice"

    @pytest.mark.asyncio
    async def test_voice_expired(self, bot_handlers):
        """Expired voice key shows error."""
        update = _make_callback_update(data="vc:s:expired1")
        ctx = _make_context()

        with patch("app.bot.handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
            )
            await bot_handlers.handle_voice_choice(update, ctx)

        update.callback_query.edit_message_text.assert_called_with(
            "❌ Данные устарели. Отправьте голосовое заново."
        )


# ── Smart Note Saving ───────────────────────────────────────────────────

class TestSaveSmartNote:
    def _patch_settings(self, tmp_dir):
        """Patch get_settings in both modules where it's imported."""
        storage_mock = MagicMock()
        storage_mock.resolved_path = tmp_dir
        notes_mock = MagicMock(vault_path=str(tmp_dir / "notes"), enabled=True)
        settings_mock = MagicMock(storage=storage_mock, notes=notes_mock)
        return patch("app.config.get_settings", return_value=settings_mock)

    @pytest.mark.asyncio
    async def test_note_capture_instant(self, bot_handlers, tmp_dir):
        """Note capture should return instantly with note ID."""
        callback = MagicMock()
        callback.edit_message_text = AsyncMock()

        # Mock CaptureService
        mock_capture = AsyncMock()
        mock_capture.capture = AsyncMock(return_value=42)

        with self._patch_settings(tmp_dir), \
             patch("app.main.get_state", return_value=mock_capture):
            await bot_handlers._save_smart_note("тест заметки", 12345, callback, source="text")

        callback.edit_message_text.assert_called_once()
        call_text = callback.edit_message_text.call_args[0][0]
        assert "Сохранено" in call_text
        assert "#42" in call_text

    @pytest.mark.asyncio
    async def test_note_source_voice(self, bot_handlers, tmp_dir):
        """Voice notes should be saved with source=voice in DB."""
        callback = MagicMock()
        callback.edit_message_text = AsyncMock()

        # No NoteAgent — fallback mode
        with self._patch_settings(tmp_dir), \
             patch("app.main.get_state", return_value=None):
            await bot_handlers._save_smart_note("голосовая заметка", 12345, callback, source="voice")

        notes = await bot_handlers.pipeline.db.list_notes()
        assert len(notes) >= 1
        assert notes[0]["source"] == "voice"

    @pytest.mark.asyncio
    async def test_note_saved_to_db(self, bot_handlers, tmp_dir):
        """Note should be saved to SQLite."""
        callback = MagicMock()
        callback.edit_message_text = AsyncMock()

        with self._patch_settings(tmp_dir), \
             patch("app.main.get_state", return_value=None):
            await bot_handlers._save_smart_note("сохрани это", 12345, callback, source="text")

        notes = await bot_handlers.pipeline.db.list_notes()
        assert len(notes) == 1
        assert notes[0]["source"] == "text"
        assert notes[0]["file_id"] == ""

    @pytest.mark.asyncio
    async def test_note_no_file_linked(self, bot_handlers, tmp_dir):
        """Notes should NOT have any file_id linked (auto-link removed)."""
        callback = MagicMock()
        callback.edit_message_text = AsyncMock()

        with self._patch_settings(tmp_dir):
            await bot_handlers._save_smart_note("заметка без файла", 12345, callback)

        notes = await bot_handlers.pipeline.db.list_notes()
        assert notes[0]["file_id"] == ""


# ── Search (no results threshold) ───────────────────────────────────────

class TestSearchNoResults:
    @pytest.mark.asyncio
    async def test_low_score_returns_nothing_found(self):
        """When all results are below MIN_SCORE, should return 'nothing found'."""
        from app.llm.search import LLMSearch

        mock_vector = MagicMock()
        mock_result = MagicMock()
        mock_result.score = 0.30  # Below MIN_SCORE (0.50)
        mock_result.text = "irrelevant text"
        mock_result.file_id = "abc"
        mock_result.metadata = {"filename": "junk.pdf"}
        mock_vector.search = AsyncMock(return_value=[mock_result])

        mock_llm = MagicMock()
        search = LLMSearch(vector_store=mock_vector, llm=mock_llm, db=None)

        result = await search.answer("несуществующий запрос")

        assert "не найдено" in result["text"]
        assert result["file_ids"] == {}

    @pytest.mark.asyncio
    async def test_no_results_at_all(self):
        """When vector search returns empty, should return 'nothing found'."""
        from app.llm.search import LLMSearch

        mock_vector = MagicMock()
        mock_vector.search = AsyncMock(return_value=[])

        search = LLMSearch(vector_store=mock_vector, llm=MagicMock(), db=None)
        result = await search.answer("пустой запрос")

        assert "не найдено" in result["text"]

    @pytest.mark.asyncio
    async def test_high_score_triggers_llm(self):
        """When result score is above MIN_SCORE, LLM should be called."""
        from app.llm.search import LLMSearch

        mock_vector = MagicMock()
        mock_result = MagicMock()
        mock_result.score = 0.85
        mock_result.text = "important document text"
        mock_result.file_id = "file1"
        mock_result.metadata = {"filename": "doc.pdf"}
        mock_vector.search = AsyncMock(return_value=[mock_result])

        mock_llm = MagicMock()
        mock_llm.search_answer = AsyncMock(return_value=MagicMock(text="Ответ на запрос"))

        search = LLMSearch(vector_store=mock_vector, llm=mock_llm, db=None)
        result = await search.answer("мой документ")

        mock_llm.search_answer.assert_called_once()
        assert result["text"] == "Ответ на запрос"
        assert "file1" in result["file_ids"]


# ── Owner Only Decorator ────────────────────────────────────────────────

class TestOwnerOnly:
    @pytest.mark.asyncio
    async def test_non_owner_blocked(self, bot_handlers):
        """Non-owner should be blocked."""
        update = _make_update(text="hello", user_id=999999)
        ctx = _make_context()

        with patch("app.bot.handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
            )
            await bot_handlers.handle_text(update, ctx)

        # Should reply with "bot is private" instead of showing buttons
        update.message.reply_text.assert_called_once()
        assert "приватный" in update.message.reply_text.call_args[0][0].lower()

    @pytest.mark.asyncio
    async def test_group_chat_blocked(self, bot_handlers):
        """Group chats should be blocked."""
        update = _make_update(text="hello", chat_type="group")
        ctx = _make_context()

        with patch("app.bot.handlers.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(
                telegram=MagicMock(owner_id=169108358, auto_delete_seconds=0, pin_code=""),
            )
            await bot_handlers.handle_text(update, ctx)

        # Should not reply at all for group chats
        update.message.reply_text.assert_not_called()


# ── Webhook Endpoint ────────────────────────────────────────────────────

class TestWebhookEndpoint:
    @pytest.mark.asyncio
    async def test_webhook_request_type_annotation(self):
        """Webhook endpoint should have Request type annotation (not bare 'request')."""
        import inspect
        from app.main import telegram_webhook
        sig = inspect.signature(telegram_webhook)
        param = sig.parameters.get("request")
        assert param is not None
        # The annotation should be Request, not inspect.Parameter.empty
        assert param.annotation is not inspect.Parameter.empty
