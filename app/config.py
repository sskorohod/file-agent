"""Configuration management — YAML + env vars via Pydantic Settings."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Sub-models ──────────────────────────────────────────────────────────────

class S3Config(BaseModel):
    bucket: str = ""
    prefix: str = "fileagent"
    region: str = "us-east-1"
    endpoint_url: str = ""       # For MinIO/Wasabi/Backblaze B2
    access_key_id: str = ""
    secret_access_key: str = ""


class GDriveConfig(BaseModel):
    credentials_json: str = ""   # Path to service account JSON
    folder_id: str = ""          # Root folder ID in Google Drive


class StorageConfig(BaseModel):
    backend: str = "local"       # "local" | "s3" | "gdrive"
    base_path: str = "~/ai-agent-files"
    max_file_size_mb: int = 50
    allowed_extensions: list[str] = [
        ".pdf", ".png", ".jpg", ".jpeg", ".heic",
        ".docx", ".txt", ".csv", ".xlsx",
    ]
    s3: S3Config = S3Config()
    gdrive: GDriveConfig = GDriveConfig()

    @property
    def resolved_path(self) -> Path:
        return Path(self.base_path).expanduser().resolve()


class TelegramConfig(BaseModel):
    bot_token: str = ""
    owner_id: int = 0  # Telegram user ID — only this user can interact with the bot
    polling_timeout: int = 30
    max_file_size_mb: int = 20
    webhook_url: str = ""  # e.g. https://fag.n8nskorx.top/telegram/webhook
    webhook_secret: str = ""  # Secret token for webhook verification
    auto_delete_seconds: int = 120  # Auto-delete bot replies after N seconds (0 = off)
    pin_code: str = ""  # 4-6 digit PIN for critical ops (delete, unlock, export)


class LLMModelConfig(BaseModel):
    model: str
    max_tokens: int = 1024
    temperature: float = 0.1
    api_base: str = ""  # custom endpoint (e.g. openai-oauth proxy)
    api_key: str = ""   # override API key for this model


class LLMRetryConfig(BaseModel):
    max_attempts: int = 3
    backoff_base: int = 2


class LLMConfig(BaseModel):
    default_provider: str = "anthropic"
    oauth_proxy_url: str = ""       # OpenAI-compatible proxy URL
    oauth_client_id: str = ""       # OAuth client ID for proxy auth
    oauth_client_secret: str = ""   # OAuth client secret
    models: dict[str, LLMModelConfig] = Field(default_factory=lambda: {
        "classification": LLMModelConfig(
            model="anthropic/claude-3-haiku-20240307",
            max_tokens=1024,
            temperature=0.1,
        ),
        "extraction": LLMModelConfig(
            model="anthropic/claude-sonnet-4-20250514",
            max_tokens=4096,
            temperature=0.2,
        ),
        "search": LLMModelConfig(
            model="anthropic/claude-sonnet-4-20250514",
            max_tokens=2048,
            temperature=0.3,
        ),
    })
    retry: LLMRetryConfig = LLMRetryConfig()
    search_prompt: str = (
        "You are an expert document analyst for a personal archive.\n\n"
        "Rules:\n"
        "- Give a DETAILED, thorough answer based on the documents found\n"
        "- Include ALL specific details: dates, names, numbers, amounts, addresses\n"
        "- Explain what the document means in practical terms for the owner\n"
        "- If the document has deadlines or action items — explain clearly what to do and by when\n"
        "- If asked about a specific field (e.g. expiry date) — give the exact value and context\n"
        "- Cite which document(s) the information comes from\n"
        "- If multiple documents are relevant — use ALL of them to give a complete picture\n"
        "- Respond in the same language as the question\n"
        "- Be precise with numbers, dates, and legal terms — never approximate when exact data exists"
    )
    classification_prompt: str = (
        "You are a document classification system.\n\n"
        "Rules:\n"
        "- summary: одно короткое предложение на русском (fallback quick reference).\n"
        "- tags: 2-4 relevant tags, lowercase\n"
        "- document_type: specific type (e.g. lab_result, invoice, passport, marketing_guide, manual, report)"
    )
    file_summary_prompt: str = (
        "Напиши краткое описание документа (2-4 предложения на русском). "
        "Что это, о чём, для чего нужен. Только факты из текста."
    )


class QdrantConfig(BaseModel):
    host: str = "localhost"
    port: int = 6333
    grpc_port: int = 6334
    collection_name: str = "file_agent"
    vector_size: int = 768
    distance: str = "Cosine"
    api_key: str = ""

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


class EmbeddingConfig(BaseModel):
    provider: str = "gemini"  # "gemini" or "local"
    model: str = "gemini-embedding-2-preview"
    local_fallback_model: str = "all-MiniLM-L6-v2"
    vector_size: int = 768  # Gemini supports 128-3072; 768 is optimal
    chunk_size_words: int = 400
    chunk_overlap_words: int = 50
    batch_size: int = 32


class SkillsConfig(BaseModel):
    directory: str = "skills/"
    auto_reload: bool = True
    reload_interval_seconds: int = 30


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    session_secret: str = ""  # auto-generated if empty
    login: str = ""           # dashboard login (email)
    password_hash: str = ""   # bcrypt hash of dashboard password
    cors_origins: list[str] = ["https://fag.n8nskorx.top"]


class DatabaseConfig(BaseModel):
    path: str = "data/agent.db"

    @property
    def resolved_path(self) -> Path:
        return Path(self.path).resolve()


class EncryptionConfig(BaseModel):
    files: bool = False        # Encrypt files on disk (AES-256-GCM)
    database: bool = False     # Encrypt sensitive DB columns
    qdrant_strip: bool = False  # Remove text from Qdrant payload


class NotesConfig(BaseModel):
    enabled: bool = True
    processing_interval_seconds: int = 900  # 15 min
    checkin_hour: int = 21  # 9 PM
    checkin_enabled: bool = True
    vault_path: str = ""  # defaults to {storage.base_path}/notes/
    inbox_path: str = ""  # defaults to {vault_path}/_inbox/
    archive_path: str = ""  # defaults to {vault_path}/_archive/
    inbox_watch_enabled: bool = True
    inbox_watch_interval: int = 10  # seconds
    expected_daily_categories: list[str] = ["food", "personal", "fitness"]
    expected_daily_signals: list[str] | None = None  # None = legacy category mode
    checkin_max_questions: int = 5  # 4 required + 1 closing
    checkin_include_closing_prompt: bool = True
    checkin_weight_frequency_days: int = 7  # ask weight once a week
    auto_embed: bool = True
    correlation_metrics: list[str] = ["mood_score", "calories", "sleep_hours", "weight_kg", "energy"]


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


# ── Main Settings ───────────────────────────────────────────────────────────

def _load_yaml(path: str | Path) -> dict[str, Any]:
    """Load YAML config file, return empty dict if not found."""
    p = Path(path)
    if p.exists():
        with open(p) as f:
            return yaml.safe_load(f) or {}
    return {}


class Settings(BaseSettings):
    """Application settings — loaded from config.yaml, overridden by env vars."""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Top-level env overrides
    telegram_bot_token: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    google_api_key: str = ""
    encryption_key: str = ""  # 64-char hex string (32 bytes), env: ENCRYPTION_KEY

    # Sections
    storage: StorageConfig = StorageConfig()
    telegram: TelegramConfig = TelegramConfig()
    llm: LLMConfig = LLMConfig()
    qdrant: QdrantConfig = QdrantConfig()
    embedding: EmbeddingConfig = EmbeddingConfig()
    skills: SkillsConfig = SkillsConfig()
    web: WebConfig = WebConfig()
    database: DatabaseConfig = DatabaseConfig()
    logging: LoggingConfig = LoggingConfig()
    encryption: EncryptionConfig = EncryptionConfig()
    notes: NotesConfig = NotesConfig()

    def __init__(self, config_path: str | Path = "config.yaml", **kwargs):
        yaml_data = _load_yaml(config_path)
        # Merge YAML into kwargs (env vars still take priority via pydantic-settings)
        merged = {**yaml_data, **kwargs}
        super().__init__(**merged)

        # Push top-level env tokens into sub-configs
        if self.telegram_bot_token:
            self.telegram.bot_token = self.telegram_bot_token

    def setup_env_keys(self):
        """Export API keys to environment for litellm to pick up."""
        if self.anthropic_api_key:
            os.environ.setdefault("ANTHROPIC_API_KEY", self.anthropic_api_key)
        if self.openai_api_key:
            os.environ.setdefault("OPENAI_API_KEY", self.openai_api_key)
        if self.google_api_key:
            os.environ.setdefault("GOOGLE_API_KEY", self.google_api_key)
            os.environ.setdefault("GEMINI_API_KEY", self.google_api_key)


@lru_cache
def get_settings(config_path: str = "config.yaml") -> Settings:
    """Cached settings singleton."""
    return Settings(config_path=config_path)


def reload_settings(config_path: str = "config.yaml") -> Settings:
    """Force reload settings (clear cache)."""
    get_settings.cache_clear()
    return get_settings(config_path)
