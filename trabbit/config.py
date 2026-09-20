from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv



def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    qbit_host: str
    qbit_username: str
    qbit_password: str

    toloka_base_url: str
    toloka_login_url: str
    toloka_username: str
    toloka_password: str
    rss_url: str

    save_path: Path
    qbit_base_path: Path
    torrent_limit_gb: float
    torrent_warning_gb: float
    managed_tag: str
    ignore_tag: str
    legacy_managed_categories: tuple[str, ...]

    auto_add_new: bool
    auto_update: bool
    watchlist_auto_add: bool
    watchlist_auto_update: bool
    readd_missing: bool
    dry_run: bool

    toloka_request_delay: float
    toloka_max_retries: int
    toloka_max_consecutive_429: int
    request_timeout: int

    db_path: Path
    rules_path: Path
    debug_save_html: bool
    debug_dir: Path

    deep_retag_batch_size: int
    deep_retag_batch_pause: float

    def max_bytes(self) -> int:
        return int(self.torrent_limit_gb * 1024**3)

    def warning_bytes(self) -> int:
        return int(self.torrent_warning_gb * 1024**3)



def load_settings(dotenv_path: str | Path = ".env") -> Settings:
    load_dotenv(dotenv_path=dotenv_path, override=False)

    toloka_base = os.getenv("TOLOKA_BASE_URL", "https://toloka.to").strip().rstrip("/")

    return Settings(
        qbit_host=os.getenv("QBIT_HOST", "http://127.0.0.1:8080").strip(),
        qbit_username=os.getenv("QBIT_USERNAME", "").strip(),
        qbit_password=os.getenv("QBIT_PASSWORD", ""),
        toloka_base_url=toloka_base,
        toloka_login_url=os.getenv("TOLOKA_LOGIN_URL", f"{toloka_base}/login.php").strip(),
        toloka_username=os.getenv("TOLOKA_USERNAME", "").strip(),
        toloka_password=os.getenv("TOLOKA_PASSWORD", ""),
        rss_url=os.getenv("RSS_URL", "").strip(),
        save_path=Path(os.getenv("SAVE_PATH", r"D:\Torent\Toloka")),
        qbit_base_path=Path(os.getenv("QBIT_BASE_PATH", r"D:\Torent\Toloka")),
        torrent_limit_gb=float(os.getenv("TORRENT_LIMIT_GB", "300")),
        torrent_warning_gb=float(os.getenv("TORRENT_WARNING_GB", "270")),
        managed_tag=os.getenv("MANAGED_TAG", "trabbit").strip(),
        ignore_tag=os.getenv("IGNORE_TAG", "trabbit-ignore").strip(),
        legacy_managed_categories=tuple(
            item.strip()
            for item in os.getenv("LEGACY_MANAGED_CATEGORIES", "TolokaSeed").split(",")
            if item.strip()
        ),
        auto_add_new=env_bool("AUTO_ADD_NEW"),
        auto_update=env_bool("AUTO_UPDATE"),
        watchlist_auto_add=env_bool("WATCHLIST_AUTO_ADD"),
        watchlist_auto_update=env_bool("WATCHLIST_AUTO_UPDATE"),
        readd_missing=env_bool("READD_MISSING", False),
        dry_run=env_bool("DRY_RUN", True),
        toloka_request_delay=float(os.getenv("TOLOKA_REQUEST_DELAY", "3.0")),
        toloka_max_retries=int(os.getenv("TOLOKA_MAX_RETRIES", "3")),
        toloka_max_consecutive_429=int(os.getenv("TOLOKA_MAX_CONSECUTIVE_429", "2")),
        request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
        db_path=Path(os.getenv("DB_PATH", "manager.db")),
        rules_path=Path(os.getenv("RULES_PATH", "config/rules.json")),
        debug_save_html=env_bool("DEBUG_SAVE_HTML", True),
        debug_dir=Path(os.getenv("DEBUG_DIR", "debug")),
        deep_retag_batch_size=max(1, int(os.getenv("DEEP_RETAG_BATCH_SIZE", "20"))),
        deep_retag_batch_pause=max(0.0, float(os.getenv("DEEP_RETAG_BATCH_PAUSE", "30"))),
    )
