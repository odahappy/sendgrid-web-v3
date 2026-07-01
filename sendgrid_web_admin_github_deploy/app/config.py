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


@dataclass
class Settings:
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
    service_token: str
    worker_interval_seconds: int
    max_send_per_tick: int
    request_timeout_seconds: int
    require_unsubscribe_for_marketing: bool
    max_recipient_upload_bytes: int
    max_template_upload_bytes: int
    max_template_files_per_upload: int


def get_settings():
    return Settings(
        server_host=os.getenv("SERVER_HOST", "127.0.0.1"),
        server_port=int_env("SERVER_PORT", 8080),
        server_auto_increment_port=bool_env("SERVER_AUTO_INCREMENT_PORT", True),
        server_port_scan_limit=int_env("SERVER_PORT_SCAN_LIMIT", 100),
        database_path=os.getenv("DATABASE_PATH", "data/web_admin_scheduler.db"),
        upload_dir=os.getenv("UPLOAD_DIR", "uploads"),
        template_storage_dir=os.getenv("TEMPLATE_STORAGE_DIR", "uploads/templates"),
        admin_username=os.getenv("ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("ADMIN_PASSWORD", "admin123456"),
        secret_key=os.getenv("SECRET_KEY", "change-this-secret-key"),
        service_token=os.getenv("SERVICE_TOKEN", "change-me-in-production"),
        worker_interval_seconds=int_env("WORKER_INTERVAL_SECONDS", 30),
        max_send_per_tick=int_env("MAX_SEND_PER_TICK", 5),
        request_timeout_seconds=int_env("REQUEST_TIMEOUT_SECONDS", 30),
        require_unsubscribe_for_marketing=bool_env("REQUIRE_UNSUBSCRIBE_FOR_MARKETING", False),
        max_recipient_upload_bytes=int_env("MAX_RECIPIENT_UPLOAD_BYTES", 5 * 1024 * 1024),
        max_template_upload_bytes=int_env("MAX_TEMPLATE_UPLOAD_BYTES", 1 * 1024 * 1024),
        max_template_files_per_upload=int_env("MAX_TEMPLATE_FILES_PER_UPLOAD", 20),
    )
