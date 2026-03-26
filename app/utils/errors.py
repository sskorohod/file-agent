"""Centralized error handling, retry logic, and user-friendly messages."""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class ErrorCategory(str, Enum):
    LLM_TIMEOUT = "llm_timeout"
    LLM_RATE_LIMIT = "llm_rate_limit"
    LLM_AUTH = "llm_auth"
    PARSE_FAILED = "parse_failed"
    STORAGE_FULL = "storage_full"
    STORAGE_ERROR = "storage_error"
    QDRANT_UNAVAILABLE = "qdrant_unavailable"
    FILE_TOO_LARGE = "file_too_large"
    UNSUPPORTED_TYPE = "unsupported_type"
    UNKNOWN = "unknown"


USER_MESSAGES = {
    ErrorCategory.LLM_TIMEOUT: "⏱ Сервер LLM не ответил вовремя. Попробуй ещё раз.",
    ErrorCategory.LLM_RATE_LIMIT: "🚦 Превышен лимит запросов к AI. Подожди минуту и попробуй снова.",
    ErrorCategory.LLM_AUTH: "🔑 Ошибка авторизации LLM. Проверь API ключи в настройках.",
    ErrorCategory.PARSE_FAILED: "📄 Не удалось прочитать файл. Возможно, он повреждён или защищён паролем.",
    ErrorCategory.STORAGE_FULL: "💾 Недостаточно места на диске.",
    ErrorCategory.STORAGE_ERROR: "💾 Ошибка сохранения файла.",
    ErrorCategory.QDRANT_UNAVAILABLE: "🔍 Векторная БД недоступна. Файл сохранён, но поиск временно не работает.",
    ErrorCategory.FILE_TOO_LARGE: "📦 Файл слишком большой. Максимум 20 MB через Telegram.",
    ErrorCategory.UNSUPPORTED_TYPE: "❓ Этот тип файла пока не поддерживается.",
    ErrorCategory.UNKNOWN: "❌ Что-то пошло не так. Попробуй ещё раз.",
}


def classify_error(error: Exception) -> ErrorCategory:
    """Classify an exception into a user-friendly category."""
    err_str = str(error).lower()
    err_type = type(error).__name__.lower()

    if "timeout" in err_str or "timed out" in err_str:
        return ErrorCategory.LLM_TIMEOUT
    if "rate_limit" in err_str or "429" in err_str or "too many" in err_str:
        return ErrorCategory.LLM_RATE_LIMIT
    if "auth" in err_str or "401" in err_str or "api key" in err_str or "invalid" in err_str:
        return ErrorCategory.LLM_AUTH
    if "no space" in err_str or "disk full" in err_str or "errno 28" in err_str:
        return ErrorCategory.STORAGE_FULL
    if "permission" in err_str or "errno 13" in err_str:
        return ErrorCategory.STORAGE_ERROR
    if "connection" in err_str and ("qdrant" in err_str or "6333" in err_str or "6334" in err_str):
        return ErrorCategory.QDRANT_UNAVAILABLE
    if "unsupported" in err_str or "no parser" in err_str:
        return ErrorCategory.UNSUPPORTED_TYPE
    if "too large" in err_str or "file size" in err_str:
        return ErrorCategory.FILE_TOO_LARGE
    if "parse" in err_type or "decode" in err_str or "corrupt" in err_str:
        return ErrorCategory.PARSE_FAILED

    return ErrorCategory.UNKNOWN


def get_user_message(error: Exception) -> str:
    """Get a user-friendly message for an exception."""
    category = classify_error(error)
    return USER_MESSAGES[category]


class PipelineError(Exception):
    """Pipeline-specific error with category."""
    def __init__(self, message: str, category: ErrorCategory = ErrorCategory.UNKNOWN, original: Exception | None = None):
        super().__init__(message)
        self.category = category
        self.original = original

    @property
    def user_message(self) -> str:
        return USER_MESSAGES[self.category]
