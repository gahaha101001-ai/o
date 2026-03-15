from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = Field(default="FastAPI Base", alias="APP_NAME")
    env: str = Field(default="development", alias="ENV")
    admin_username: str = Field(default="admin", alias="ADMIN_USERNAME")
    admin_password: str = Field(default="admin", alias="ADMIN_PASSWORD")
    admin_session_secret: str = Field(
        default="replace-this-secret", alias="ADMIN_SESSION_SECRET"
    )
    admin_session_ttl_seconds: int = Field(
        default=28800, alias="ADMIN_SESSION_TTL_SECONDS"
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    mongo_uri: str = Field(default="mongodb://localhost:27017", alias="MONGO_URI")
    mongo_db_name: str = Field(default="fastapi_base", alias="MONGO_DB_NAME")
    mongo_submissions_collection: str = Field(
        default="submissions", alias="MONGO_SUBMISSIONS_COLLECTION"
    )
    mongo_visitors_collection: str = Field(
        default="visitors", alias="MONGO_VISITORS_COLLECTION"
    )
    mongo_settings_collection: str = Field(
        default="settings", alias="MONGO_SETTINGS_COLLECTION"
    )
    support_whatsapp_number: str = Field(
        default="", alias="SUPPORT_WHATSAPP_NUMBER"
    )
    online_users_key: str = Field(
        default="fastapi-base:online-users", alias="ONLINE_USERS_KEY"
    )
    online_user_ttl_seconds: int = Field(
        default=5, alias="ONLINE_USER_TTL_SECONDS"
    )
    online_heartbeat_interval_seconds: int = Field(
        default=2, alias="ONLINE_HEARTBEAT_INTERVAL_SECONDS"
    )
    online_presence_broadcast_interval_seconds: float = Field(
        default=1.0, alias="ONLINE_PRESENCE_BROADCAST_INTERVAL_SECONDS"
    )
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["*"], alias="ALLOWED_HOSTS"
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="CORS_ORIGINS"
    )

    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @field_validator("env", mode="before")
    @classmethod
    def normalize_env(cls, value: object) -> str:
        return str(value or "development").strip().lower()

    @field_validator("allowed_hosts", "cors_origins", mode="before")
    @classmethod
    def parse_comma_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            cleaned = [item.strip() for item in value.split(",") if item.strip()]
            return cleaned
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        if self.env != "production":
            return self

        weak_secrets = {"", "replace-this-secret", "change-this-in-production"}
        weak_passwords = {"", "admin", "password", "123456", "1"}

        if self.admin_session_secret.strip() in weak_secrets:
            raise ValueError(
                "ADMIN_SESSION_SECRET must be set to a strong unique value in production."
            )
        if self.admin_password.strip().lower() in weak_passwords:
            raise ValueError(
                "ADMIN_PASSWORD must be changed from the default before production deployment."
            )
        if not self.allowed_hosts or "*" in self.allowed_hosts:
            raise ValueError(
                "ALLOWED_HOSTS must list your real hostnames in production and cannot include '*'."
            )
        return self


settings = Settings()
