"""Tests for error classification."""

from app.utils.errors import classify_error, ErrorCategory, get_user_message


class TestErrorClassification:
    def test_timeout(self):
        assert classify_error(TimeoutError("request timed out")) == ErrorCategory.LLM_TIMEOUT

    def test_rate_limit(self):
        assert classify_error(Exception("429 Too Many Requests")) == ErrorCategory.LLM_RATE_LIMIT

    def test_auth(self):
        assert classify_error(Exception("401 Invalid API key")) == ErrorCategory.LLM_AUTH

    def test_disk_full(self):
        assert classify_error(OSError("No space left on device (errno 28)")) == ErrorCategory.STORAGE_FULL

    def test_unknown(self):
        assert classify_error(Exception("something weird")) == ErrorCategory.UNKNOWN

    def test_user_messages(self):
        for cat in ErrorCategory:
            msg = get_user_message(Exception("test"))
            assert isinstance(msg, str)
            assert len(msg) > 0
