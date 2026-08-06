import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def bool_env(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def int_env(name, default):
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    environment: str
    server_host: str
    server_port: int
    server_auto_increment_port: bool
    server_port_scan_limit: int
    database_path: str
    upload_dir: str
    template_storage_dir: str
    admin_username: str
    admin_password: str
    secret_key: str
    data_encryption_key: str
    service_token: str
    session_cookie_secure: bool
    session_max_age_seconds: int
    login_max_attempts: int
    login_window_seconds: int
    worker_interval_seconds: int
    worker_claim_timeout_seconds: int
    max_send_per_tick: int
    request_timeout_seconds: int
    require_unsubscribe_for_marketing: bool
    max_recipient_upload_bytes: int
    max_recipient_files_per_upload: int
    max_template_upload_bytes: int
    max_template_files_per_upload: int
    max_webhook_bytes: int
    allow_webhook_query_token: bool
    store_send_request_body: bool


def get_settings():
    secret_key = os.getenv("SECRET_KEY", "change-this-secret-key")
    return Settings(
        environment=os.getenv("ENVIRONMENT", "development").strip().lower(),
        server_host=os.getenv("SERVER_HOST", "127.0.0.1"),
        server_port=int_env("SERVER_PORT", 8080),
        server_auto_increment_port=bool_env("SERVER_AUTO_INCREMENT_PORT", True),
        server_port_scan_limit=int_env("SERVER_PORT_SCAN_LIMIT", 100),
        database_path=os.getenv("DATABASE_PATH", "data/web_admin_scheduler.db"),
        upload_dir=os.getenv("UPLOAD_DIR", "uploads"),
        template_storage_dir=os.getenv("TEMPLATE_STORAGE_DIR", "uploads/templates"),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", "admin123456"),
        secret_key=secret_key,
        data_encryption_key=os.getenv("DATA_ENCRYPTION_KEY", secret_key),
        service_token=os.getenv("SERVICE_TOKEN", "change-me-in-production"),
        session_cookie_secure=bool_env("SESSION_COOKIE_SECURE", False),
        session_max_age_seconds=max(300, int_env("SESSION_MAX_AGE_SECONDS", 8 * 60 * 60)),
        login_max_attempts=max(1, int_env("LOGIN_MAX_ATTEMPTS", 5)),
        login_window_seconds=max(60, int_env("LOGIN_WINDOW_SECONDS", 15 * 60)),
        worker_interval_seconds=max(1, int_env("WORKER_INTERVAL_SECONDS", 30)),
        worker_claim_timeout_seconds=max(60, int_env("WORKER_CLAIM_TIMEOUT_SECONDS", 300)),
        max_send_per_tick=max(1, int_env("MAX_SEND_PER_TICK", 5)),
        request_timeout_seconds=max(5, int_env("REQUEST_TIMEOUT_SECONDS", 30)),
        require_unsubscribe_for_marketing=bool_env("REQUIRE_UNSUBSCRIBE_FOR_MARKETING", False),
        max_recipient_upload_bytes=max(1024, int_env("MAX_RECIPIENT_UPLOAD_BYTES", 5 * 1024 * 1024)),
        max_recipient_files_per_upload=max(1, int_env("MAX_RECIPIENT_FILES_PER_UPLOAD", 20)),
        max_template_upload_bytes=max(1024, int_env("MAX_TEMPLATE_UPLOAD_BYTES", 1 * 1024 * 1024)),
        max_template_files_per_upload=max(1, int_env("MAX_TEMPLATE_FILES_PER_UPLOAD", 20)),
        max_webhook_bytes=max(1024, int_env("MAX_WEBHOOK_BYTES", 2 * 1024 * 1024)),
        allow_webhook_query_token=bool_env("ALLOW_WEBHOOK_QUERY_TOKEN", False),
        store_send_request_body=bool_env("STORE_SEND_REQUEST_BODY", False),
    )


def validate_settings(settings=None):
    """Reject known unsafe defaults when explicitly running in production."""
    s = settings or get_settings()
    if s.environment != "production":
        return

    errors = []
    if s.admin_password in ("admin123456", "CHANGE_THIS_ADMIN_PASSWORD") or len(s.admin_password) < 12:
        errors.append("ADMIN_PASSWORD must be changed and contain at least 12 characters")
    if s.secret_key in ("change-this-secret-key", "CHANGE_THIS_LONG_RANDOM_SECRET_KEY") or len(s.secret_key) < 32:
        errors.append("SECRET_KEY must be a random value of at least 32 characters")
    if s.data_encryption_key in ("change-this-secret-key", "CHANGE_THIS_LONG_RANDOM_DATA_KEY") or len(s.data_encryption_key) < 32:
        errors.append("DATA_ENCRYPTION_KEY must be a random value of at least 32 characters")
    if s.data_encryption_key == s.secret_key:
        errors.append("DATA_ENCRYPTION_KEY must be different from SECRET_KEY")
    if s.service_token in ("change-me-in-production", "CHANGE_THIS_LONG_RANDOM_SERVICE_TOKEN") or len(s.service_token) < 24:
        errors.append("SERVICE_TOKEN must be a random value of at least 24 characters")
    if errors:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))
