from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import bencodepy
import feedparser
import qbittorrentapi
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# Configuration
# ============================================================

QBIT_HOST = os.getenv("QBIT_HOST", "http://127.0.0.1:8080").strip()
QBIT_USERNAME = os.getenv("QBIT_USERNAME", "").strip()
QBIT_PASSWORD = os.getenv("QBIT_PASSWORD", "")

RSS_URL = os.getenv("RSS_URL", "").strip()

TOLOKA_BASE_URL = os.getenv("TOLOKA_BASE_URL", "https://toloka.to").strip()
TOLOKA_LOGIN_URL = os.getenv("TOLOKA_LOGIN_URL", f"{TOLOKA_BASE_URL}/login.php").strip()
TOLOKA_USERNAME = os.getenv("TOLOKA_USERNAME", "").strip()
TOLOKA_PASSWORD = os.getenv("TOLOKA_PASSWORD", "")

SAVE_PATH = Path(os.getenv("SAVE_PATH", r"D:\Torent"))
QBIT_CATEGORY = os.getenv("QBIT_CATEGORY", "TolokaSeed").strip()
TORRENT_LIMIT_GB = float(os.getenv("TORRENT_LIMIT_GB", "250"))

AUTO_ADD_NEW = os.getenv("AUTO_ADD_NEW", "false").lower() in {"1", "true", "yes", "on"}
AUTO_UPDATE = os.getenv("AUTO_UPDATE", "false").lower() in {"1", "true", "yes", "on"}
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in {"1", "true", "yes", "on"}

WATCHLIST_AUTO_ADD = os.getenv("WATCHLIST_AUTO_ADD", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
WATCHLIST_AUTO_UPDATE = os.getenv("WATCHLIST_AUTO_UPDATE", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

DEBUG_SAVE_HTML = os.getenv("DEBUG_SAVE_HTML", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEBUG_DIR = Path(os.getenv("DEBUG_DIR", "debug"))

DB_PATH = Path(os.getenv("DB_PATH", "manager.db"))
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "30"))
TOLOKA_REQUEST_DELAY = float(os.getenv("TOLOKA_REQUEST_DELAY", "3.0"))
TOLOKA_MAX_RETRIES = int(os.getenv("TOLOKA_MAX_RETRIES", "3"))
TOLOKA_MAX_CONSECUTIVE_429 = int(os.getenv("TOLOKA_MAX_CONSECUTIVE_429", "2"))

# ============================================================
# Logging / HTTP session
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("TolokaSeedManager")

HTTP_SESSION = requests.Session()
HTTP_SESSION.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0 Safari/537.36 TolokaSeedManager/1.1"
        ),
        "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
    }
)

_LAST_TOLOKA_REQUEST = 0.0


class TolokaRateLimitError(RuntimeError):
    pass


# ============================================================
# Helpers
# ============================================================


def human_size(size: int | float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_local() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def sanitize_filename(filename: str) -> str:
    filename = re.sub(r'[<>:"/\\|?*]', "_", filename.strip())
    filename = re.sub(r"\s+", " ", filename).rstrip(". ")
    return filename[:180] or "torrent"


def save_debug_html(topic_id: str | None, html: str) -> None:
    if not DEBUG_SAVE_HTML:
        return
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    identifier = topic_id or "unknown"
    path = DEBUG_DIR / f"topic_{identifier}.html"
    try:
        path.write_text(html, encoding="utf-8")
        log.info("  HTML збережено: %s", path)
    except OSError as exc:
        log.warning("  Не вдалося зберегти HTML: %s", exc)


# ============================================================
# Toloka HTTP / rate limiting
# ============================================================


def _respect_rate_limit() -> None:
    global _LAST_TOLOKA_REQUEST
    elapsed = time.monotonic() - _LAST_TOLOKA_REQUEST
    remaining = TOLOKA_REQUEST_DELAY - elapsed
    if remaining > 0:
        time.sleep(remaining)


def toloka_get(
    url: str,
    *,
    auth: tuple[str, str] | None = None,
    **kwargs: Any,
) -> requests.Response:
    """GET Toloka politely and stop rather than hammering on 429."""
    global _LAST_TOLOKA_REQUEST

    last_response: requests.Response | None = None

    for attempt in range(TOLOKA_MAX_RETRIES + 1):
        _respect_rate_limit()

        response = HTTP_SESSION.get(
            url,
            auth=auth,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            **kwargs,
        )
        _LAST_TOLOKA_REQUEST = time.monotonic()
        last_response = response

        if response.status_code != 429:
            return response

        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 5.0 * (2**attempt)
        except ValueError:
            delay = 5.0 * (2**attempt)

        delay = min(max(delay, 5.0), 60.0)
        log.warning(
            "Toloka повернула 429 Too Many Requests. "
            "Чекаю %.1f с перед повтором (%d/%d).",
            delay,
            attempt + 1,
            TOLOKA_MAX_RETRIES,
        )
        time.sleep(delay)

    raise TolokaRateLimitError(
        f"Toloka продовжує повертати 429 після {TOLOKA_MAX_RETRIES + 1} спроб."
    )


# ============================================================
# Toloka login
# ============================================================


def login_to_toloka() -> None:
    """Login to Toloka using the current phpBB-style form fields."""

    if not TOLOKA_USERNAME or not TOLOKA_PASSWORD:
        raise RuntimeError(
            "TOLOKA_USERNAME / TOLOKA_PASSWORD не задані у .env. "
            "Толока вимагає авторизацію для завантаження torrent-файлів."
        )

    log.info("Виконую вхід у Толоку як %s", TOLOKA_USERNAME)

    # First visit the login page so the session receives any initial cookies.
    response = toloka_get(
        TOLOKA_LOGIN_URL,
        headers={"Referer": TOLOKA_BASE_URL + "/"},
    )
    response.raise_for_status()

    # Toloka's current login form uses these phpBB-style field names.
    # This is more reliable than trying to infer the password input from HTML,
    # because the site may render/alter the form markup while preserving the
    # actual POST contract.
    data = {
        "username": TOLOKA_USERNAME,
        "password": TOLOKA_PASSWORD,
        "autologin": "on",
        "ssl": "on",
        "redirect": "",
        "login": "Вхід",
    }

    _respect_rate_limit()

    try:
        submit = HTTP_SESSION.post(
            TOLOKA_LOGIN_URL,
            data=data,
            headers={
                "Referer": response.url,
                "Origin": TOLOKA_BASE_URL,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Помилка входу до Toloka: {exc}") from exc

    global _LAST_TOLOKA_REQUEST
    _LAST_TOLOKA_REQUEST = time.monotonic()

    if submit.status_code == 429:
        raise TolokaRateLimitError(
            "Toloka повернула 429 під час входу. Запустіть скрипт пізніше."
        )

    if submit.status_code not in {200, 302, 303}:
        submit.raise_for_status()

    # A redirect is the normal successful-login response.
    if submit.status_code in {302, 303}:
        log.info(
            "Toloka прийняла форму входу (HTTP %s).",
            submit.status_code,
        )

        # Follow the redirect with the same session and verify that the
        # authenticated page is actually available.
        location = submit.headers.get("Location") or "/"
        verify_url = urljoin(
            TOLOKA_BASE_URL + "/",
            location,
        )

        verify = toloka_get(
            verify_url,
            headers={"Referer": TOLOKA_LOGIN_URL},
        )

        if verify.status_code == 429:
            raise TolokaRateLimitError(
                "Toloka повернула 429 під час перевірки входу. "
                "Запустіть скрипт пізніше."
            )

        verify.raise_for_status()

        log.info("Вхід у Толоку виконано.")
        return

    # HTTP 200 generally means the login form was returned again, often due
    # to invalid credentials. Keep the server's response for diagnostics.
    body_lower = submit.text.lower()

    failed_markers = [
        "будь ласка, введіть ваш логін і пароль",
        "такий псевдонім не існує",
        "не збігається пароль",
        "невірний пароль",
        "неправильний пароль",
        "невірний логін",
    ]

    if any(marker in body_lower for marker in failed_markers):
        raise RuntimeError(
            "Toloka не прийняла логін/пароль. Перевірте "
            "TOLOKA_USERNAME і TOLOKA_PASSWORD у .env."
        )

    # Save the response when Toloka changes its login flow.
    if DEBUG_SAVE_HTML:
        DEBUG_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        path = DEBUG_DIR / "toloka_login_response.html"

        try:
            path.write_text(
                submit.text,
                encoding="utf-8",
            )
            log.info(
                "  Відповідь login.php збережено: %s",
                path,
            )
        except OSError as exc:
            log.warning(
                "  Не вдалося зберегти login response: %s",
                exc,
            )

    raise RuntimeError(
        f"Toloka повернула HTTP {submit.status_code}, але "
        "успішний вхід не вдалося підтвердити. "
        "Перевірте debug/toloka_login_response.html."
    )


# ============================================================
# SQLite + migration
# ============================================================


def init_database() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    conn.execute("""
        CREATE TABLE IF NOT EXISTS rss_items (
            item_key TEXT PRIMARY KEY,
            topic_id TEXT,
            title TEXT NOT NULL,
            topic_url TEXT,
            torrent_url TEXT,
            fingerprint TEXT,
            info_hash TEXT,
            size_bytes INTEGER,
            last_seen TEXT NOT NULL,
            last_added TEXT,
            last_update_seen TEXT,
            status TEXT
        )
        """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchlist (
            topic_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            topic_url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            added_at TEXT NOT NULL,
            last_checked TEXT,
            last_fingerprint TEXT,
            last_info_hash TEXT,
            last_size_bytes INTEGER,
            last_status TEXT
        )
        """)

    # Migrate databases created by previous versions.
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(rss_items)").fetchall()
    }

    migrations = {
        "topic_url": "TEXT",
        "info_hash": "TEXT",
    }

    for column, sql_type in migrations.items():
        if column not in columns:
            log.info("Оновлюю SQLite: додаю колонку %s", column)
            conn.execute(f"ALTER TABLE rss_items ADD COLUMN {column} {sql_type}")

    conn.commit()
    return conn


def get_item(
    conn: sqlite3.Connection,
    item_key: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM rss_items WHERE item_key = ?",
        (item_key,),
    ).fetchone()


def save_item(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    topic_id: str | None,
    title: str,
    topic_url: str | None,
    torrent_url: str | None,
    fingerprint: str | None,
    info_hash: str | None,
    size_bytes: int | None,
    status: str,
    last_added: str | None = None,
    last_update_seen: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO rss_items (
            item_key, topic_id, title, topic_url, torrent_url,
            fingerprint, info_hash, size_bytes, last_seen,
            last_added, last_update_seen, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(item_key) DO UPDATE SET
            topic_id = excluded.topic_id,
            title = excluded.title,
            topic_url = excluded.topic_url,
            torrent_url = excluded.torrent_url,
            fingerprint = excluded.fingerprint,
            info_hash = excluded.info_hash,
            size_bytes = excluded.size_bytes,
            last_seen = excluded.last_seen,
            last_added = COALESCE(excluded.last_added, rss_items.last_added),
            last_update_seen = COALESCE(
                excluded.last_update_seen,
                rss_items.last_update_seen
            ),
            status = excluded.status
        """,
        (
            item_key,
            topic_id,
            title,
            topic_url,
            torrent_url,
            fingerprint,
            info_hash,
            size_bytes,
            now_iso(),
            last_added,
            last_update_seen,
            status,
        ),
    )
    conn.commit()


# ============================================================
# Watchlist
# ============================================================


def normalize_topic_input(value: str) -> tuple[str, str]:
    """Return (topic_id, canonical_topic_url) from tXXXXX or a Toloka URL."""
    value = value.strip()

    match = re.search(r"(?:/t|^t?)(\d+)(?:$|[/?#])", value, re.IGNORECASE)
    if not match:
        raise ValueError(f"Не вдалося визначити Topic ID з: {value}")

    topic_id = match.group(1)
    return topic_id, f"{TOLOKA_BASE_URL}/t{topic_id}"


def get_watch_item(
    conn: sqlite3.Connection,
    topic_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM watchlist WHERE topic_id = ?",
        (topic_id,),
    ).fetchone()


def add_watch_item(
    conn: sqlite3.Connection,
    topic_id: str,
    title: str | None = None,
) -> None:
    topic_url = f"{TOLOKA_BASE_URL}/t{topic_id}"
    existing = get_watch_item(conn, topic_id)
    display_title = title or (existing["title"] if existing else f"Topic {topic_id}")

    conn.execute(
        """
        INSERT INTO watchlist (
            topic_id,
            title,
            topic_url,
            enabled,
            added_at
        )
        VALUES (?, ?, ?, 1, ?)
        ON CONFLICT(topic_id) DO UPDATE SET
            title = excluded.title,
            topic_url = excluded.topic_url,
            enabled = 1
        """,
        (
            topic_id,
            display_title,
            topic_url,
            now_iso(),
        ),
    )
    conn.commit()
    log.info(
        "Watchlist: додано t%s → %s",
        topic_id,
        display_title,
    )


def remove_watch_item(
    conn: sqlite3.Connection,
    topic_id: str,
) -> bool:
    cursor = conn.execute(
        "DELETE FROM watchlist WHERE topic_id = ?",
        (topic_id,),
    )
    conn.commit()

    if cursor.rowcount:
        log.info(
            "Watchlist: видалено t%s",
            topic_id,
        )
        return True

    log.warning(
        "Watchlist: t%s не знайдено.",
        topic_id,
    )
    return False


def list_watch_items(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return list(conn.execute("""
            SELECT *
            FROM watchlist
            ORDER BY enabled DESC, added_at DESC
            """).fetchall())


def update_watch_item(
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    title: str,
    topic_url: str,
    fingerprint: str | None = None,
    info_hash: str | None = None,
    size_bytes: int | None = None,
    status: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE watchlist
        SET title = ?,
            topic_url = ?,
            last_checked = ?,
            last_fingerprint = COALESCE(?, last_fingerprint),
            last_info_hash = COALESCE(?, last_info_hash),
            last_size_bytes = COALESCE(?, last_size_bytes),
            last_status = COALESCE(?, last_status)
        WHERE topic_id = ?
        """,
        (
            title,
            topic_url,
            now_iso(),
            fingerprint,
            info_hash,
            size_bytes,
            status,
            topic_id,
        ),
    )
    conn.commit()


def print_watchlist(
    conn: sqlite3.Connection,
) -> None:
    rows = list_watch_items(conn)

    print()
    print("=" * 72)
    print(" Watchlist")
    print("=" * 72)

    if not rows:
        print("Поки що watchlist порожній.")
        print("Додати: python main.py --watch-add 696017")
        print()
        return

    for row in rows:
        state = "ON " if row["enabled"] else "OFF"
        last_checked = row["last_checked"] or "ще не перевірявся"
        status = row["last_status"] or "-"
        print(
            f"[{state}] t{row['topic_id']} | "
            f"{row['title']} | "
            f"status={status} | "
            f"last={last_checked}"
        )

    print()


# ============================================================
# Watchlist update logic
# ============================================================


def sync_watch_item(
    client: qbittorrentapi.Client,
    conn: sqlite3.Connection,
    *,
    topic_id: str,
    title: str,
    topic_url: str,
    torrent_data: bytes,
    torrent_title: str,
    fingerprint: str,
    info_hash: str,
    size_bytes: int,
    used_space: int,
    max_space: int,
) -> int:
    """Update a watchlist record and optionally seed first/new versions."""
    watch = get_watch_item(conn, topic_id)
    if watch is None:
        return used_space

    old_fingerprint = watch["last_fingerprint"]

    # First observation establishes the baseline.
    if not old_fingerprint:
        log.info(
            "  [WATCH] t%s: встановлюю початковий fingerprint.",
            topic_id,
        )

        status = "baseline_set"

        if WATCHLIST_AUTO_ADD:
            if find_existing_info_hash(client, info_hash):
                log.info("  [WATCH] Початкова версія вже є в qBittorrent.")
                status = "baseline_already_in_qbit"
            elif used_space + size_bytes <= max_space:
                log.info("  [WATCH] WATCHLIST_AUTO_ADD=true → додаю початкову версію.")
                add_torrent(
                    client,
                    torrent_data,
                    torrent_title,
                )
                if not DRY_RUN:
                    used_space += size_bytes
                    status = "baseline_added"
                else:
                    status = "baseline_would_add"
            else:
                log.warning("  [WATCH] Початкова версія не вміщується у ліміт.")
                status = "baseline_storage_limit"

        update_watch_item(
            conn,
            topic_id=topic_id,
            title=title,
            topic_url=topic_url,
            fingerprint=fingerprint,
            info_hash=info_hash,
            size_bytes=size_bytes,
            status=status,
        )
        return used_space

    # Same torrent version. If it was previously only observed in
    # dry-run / non-adding mode and is missing in qBittorrent, allow
    # WATCHLIST_AUTO_ADD to add that known baseline now.
    if old_fingerprint == fingerprint:
        if WATCHLIST_AUTO_ADD and not find_existing_info_hash(client, info_hash):
            if used_space + size_bytes <= max_space:
                log.info("  [WATCH] Та сама базова версія ще не в qBittorrent → додаю.")
                add_torrent(
                    client,
                    torrent_data,
                    torrent_title,
                )
                if not DRY_RUN:
                    used_space += size_bytes
                    status = "baseline_added"
                else:
                    status = "baseline_would_add"
            else:
                log.warning("  [WATCH] Базова версія не вміщується у ліміт.")
                status = "baseline_storage_limit"

            update_watch_item(
                conn,
                topic_id=topic_id,
                title=title,
                topic_url=topic_url,
                fingerprint=fingerprint,
                info_hash=info_hash,
                size_bytes=size_bytes,
                status=status,
            )
            return used_space

        update_watch_item(
            conn,
            topic_id=topic_id,
            title=title,
            topic_url=topic_url,
            status="unchanged",
        )
        return used_space

    # Different .torrent file for the same watched topic.
    log.warning(
        "  [WATCH UPDATE] t%s: знайдено нову версію torrent!",
        topic_id,
    )
    log.warning(
        "  [WATCH UPDATE] Було: %s",
        old_fingerprint,
    )
    log.warning(
        "  [WATCH UPDATE] Стало: %s",
        fingerprint,
    )

    status = "update_detected"

    if WATCHLIST_AUTO_UPDATE:
        if find_existing_info_hash(client, info_hash):
            log.info("  [WATCH UPDATE] Нова версія вже є в qBittorrent.")
            status = "update_already_in_qbit"
        elif used_space + size_bytes <= max_space:
            log.info("  [WATCH UPDATE] WATCHLIST_AUTO_UPDATE=true → додаю нову версію.")
            add_torrent(
                client,
                torrent_data,
                torrent_title,
            )
            if not DRY_RUN:
                used_space += size_bytes
                status = "update_added"
            else:
                status = "update_would_add"
        else:
            log.warning("  [WATCH UPDATE] Нова версія не вміщується у ліміт.")
            status = "update_storage_limit"
    else:
        log.info(
            "  [WATCH UPDATE] WATCHLIST_AUTO_UPDATE=false → тільки фіксую оновлення."
        )

    # Save the new fingerprint even when automatic adding is disabled,
    # so the same version is not reported on every daily run.
    update_watch_item(
        conn,
        topic_id=topic_id,
        title=title,
        topic_url=topic_url,
        fingerprint=fingerprint,
        info_hash=info_hash,
        size_bytes=size_bytes,
        status=status,
    )
    return used_space


# ============================================================
# RSS
# ============================================================


def fetch_rss() -> feedparser.FeedParserDict:
    if not RSS_URL:
        raise RuntimeError("RSS_URL не заданий у .env")

    log.info("Читаю RSS: %s", RSS_URL)
    response = toloka_get(RSS_URL)
    response.raise_for_status()

    feed = feedparser.parse(response.content)
    if getattr(feed, "bozo", False):
        log.warning(
            "RSS parser повідомив про проблему: %s",
            getattr(feed, "bozo_exception", "unknown"),
        )

    log.info("RSS записів отримано: %d", len(feed.entries))
    return feed


# ============================================================
# RSS item parsing
# ============================================================


def extract_topic_id(entry: Any) -> str | None:
    for value in [entry.get("id", ""), entry.get("link", "")]:
        if not value:
            continue
        match = re.search(r"/t(\d+)(?:[-#/]|$)", str(value), re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def extract_item_key(entry: Any) -> str:
    topic_id = extract_topic_id(entry)
    if topic_id:
        return f"topic:{topic_id}"
    if entry.get("id"):
        return f"id:{entry['id']}"
    if entry.get("link"):
        return f"link:{entry['link']}"
    return f"title:{entry.get('title', '')}"


# ============================================================
# Resolve torrent URL from authenticated topic page
# ============================================================


def resolve_torrent_url(
    topic_url: str,
    topic_id: str | None = None,
) -> str | None:
    log.info("  Відкриваю тему: %s", topic_url)

    response = toloka_get(
        topic_url,
        headers={"Referer": TOLOKA_BASE_URL + "/"},
    )
    response.raise_for_status()

    html = response.text
    soup = BeautifulSoup(html, "html.parser")

    # Preferred: actual download link from the topic page.
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        full_url = urljoin(response.url, href)
        lower = full_url.lower()
        if "download.php" in lower or ".torrent" in lower:
            log.info("  Знайдено torrent URL: %s", full_url)
            return full_url

    # Fallback: search raw HTML.
    patterns = [
        r'https?://[^"\'<>\s]+download\.php\?[^"\'<>\s]+',
        r'/download\.php\?[^"\'<>\s]+',
        r'https?://[^"\'<>\s]+\.torrent[^"\'<>\s]*',
        r'/[^"\'<>\s]+\.torrent[^"\'<>\s]*',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            url = urljoin(response.url, match.group(0))
            log.info("  Знайдено torrent URL через HTML: %s", url)
            return url

    # Magnet fallback.
    for link in soup.find_all("a", href=True):
        href = link["href"].strip()
        if href.lower().startswith("magnet:"):
            log.info("  Знайдено magnet URL")
            return href

    log.warning("  Не знайшов .torrent/download.php/magnet.")
    save_debug_html(topic_id, html)
    return None


# ============================================================
# Torrent parsing
# ============================================================


def torrent_size(torrent_data: bytes) -> int:
    decoded = bencodepy.decode(torrent_data)
    info = decoded.get(b"info")
    if not isinstance(info, dict):
        raise ValueError("У .torrent немає info")

    if b"length" in info:
        return int(info[b"length"])

    return sum(int(file_info[b"length"]) for file_info in info.get(b"files", []))


def torrent_name(torrent_data: bytes) -> str:
    decoded = bencodepy.decode(torrent_data)
    info = decoded.get(b"info")
    if not isinstance(info, dict):
        return "torrent"
    name = info.get(b"name", b"torrent")
    return (
        name.decode("utf-8", errors="replace") if isinstance(name, bytes) else str(name)
    )


def torrent_fingerprint(torrent_data: bytes) -> str:
    return hashlib.sha256(torrent_data).hexdigest()


def torrent_info_hash(torrent_data: bytes) -> str:
    decoded = bencodepy.decode(torrent_data)
    info = decoded.get(b"info")
    if info is None:
        raise ValueError("Torrent не містить info")
    return hashlib.sha1(bencodepy.encode(info)).hexdigest()


def download_torrent(url: str) -> tuple[bytes, int, str, str, str]:
    if url.lower().startswith("magnet:"):
        raise ValueError(
            "Magnet поки що не використовується для попередньої оцінки розміру."
        )

    log.info("  Завантажую .torrent: %s", url)
    response = toloka_get(url, headers={"Referer": TOLOKA_BASE_URL + "/"})
    response.raise_for_status()

    data = response.content
    try:
        decoded = bencodepy.decode(data)
    except Exception as exc:
        preview = data[:200].decode("utf-8", errors="replace")
        raise ValueError(
            f"Сервер не повернув валідний .torrent. Початок: {preview!r}"
        ) from exc

    if not isinstance(decoded, dict) or b"info" not in decoded:
        raise ValueError("Отримані дані не є валідним torrent-файлом.")

    return (
        data,
        torrent_size(data),
        torrent_name(data),
        torrent_fingerprint(data),
        torrent_info_hash(data),
    )


# ============================================================
# qBittorrent
# ============================================================


def connect_qbittorrent() -> qbittorrentapi.Client:
    client = qbittorrentapi.Client(
        host=QBIT_HOST,
        username=QBIT_USERNAME,
        password=QBIT_PASSWORD,
    )
    client.auth_log_in()
    log.info("Підключено до qBittorrent %s", client.app.version)
    log.info("Web API: %s", client.app.web_api_version)
    return client


def ensure_category(client: qbittorrentapi.Client) -> None:
    categories = client.torrent_categories.categories
    if QBIT_CATEGORY in categories:
        log.info("Категорія вже існує: %s", QBIT_CATEGORY)
        return
    log.info("Створюю категорію: %s", QBIT_CATEGORY)
    client.torrent_categories.create_category(
        name=QBIT_CATEGORY,
        save_path=str(SAVE_PATH),
    )


def get_qbit_torrents(client: qbittorrentapi.Client) -> list[Any]:
    return list(client.torrents_info(category=QBIT_CATEGORY))


def get_used_space(client: qbittorrentapi.Client) -> int:
    return sum(int(t.total_size) for t in get_qbit_torrents(client))


def find_existing_info_hash(client: qbittorrentapi.Client, info_hash: str) -> bool:
    target = info_hash.lower()
    return any(str(t.hash).lower() == target for t in get_qbit_torrents(client))


def add_torrent(
    client: qbittorrentapi.Client,
    torrent_data: bytes,
    torrent_name_value: str,
) -> None:
    filename = sanitize_filename(torrent_name_value) + ".torrent"

    if DRY_RUN:
        log.info("  [DRY RUN] Додав би: %s", torrent_name_value)
        return

    result = client.torrents_add(
        torrent_files={filename: torrent_data},
        save_path=str(SAVE_PATH),
        category=QBIT_CATEGORY,
        is_paused=False,
    )
    log.info("  qBittorrent: %s", result)


# ============================================================
# Process one RSS entry
# ============================================================


def process_entry(
    client: qbittorrentapi.Client,
    conn: sqlite3.Connection,
    entry: Any,
    used_space: int,
    max_space: int,
) -> int:
    title = str(entry.get("title", "(без назви)")).strip()
    item_key = extract_item_key(entry)
    topic_id = extract_topic_id(entry)
    topic_url = entry.get("link")

    log.info("Перевіряю: %s", title)
    log.info("  Topic ID: %s", topic_id or "невідомий")

    if not topic_url:
        save_item(
            conn,
            item_key=item_key,
            topic_id=topic_id,
            title=title,
            topic_url=None,
            torrent_url=None,
            fingerprint=None,
            info_hash=None,
            size_bytes=None,
            status="no_topic_url",
        )
        return used_space

    existing = get_item(conn, item_key)

    torrent_url = resolve_torrent_url(topic_url, topic_id)
    if not torrent_url:
        save_item(
            conn,
            item_key=item_key,
            topic_id=topic_id,
            title=title,
            topic_url=topic_url,
            torrent_url=None,
            fingerprint=None,
            info_hash=None,
            size_bytes=None,
            status="no_torrent_url",
        )
        return used_space

    try:
        torrent_data, size_bytes, torrent_title, fingerprint, info_hash = (
            download_torrent(torrent_url)
        )
    except Exception as exc:
        log.error("  Помилка завантаження torrent: %s", exc)
        save_item(
            conn,
            item_key=item_key,
            topic_id=topic_id,
            title=title,
            topic_url=topic_url,
            torrent_url=torrent_url,
            fingerprint=None,
            info_hash=None,
            size_bytes=None,
            status="torrent_download_error",
        )
        return used_space

    log.info("  Torrent: %s", torrent_title)
    log.info("  Розмір: %s", human_size(size_bytes))
    log.info("  Info hash: %s", info_hash)

    used_space = sync_watch_item(
        client,
        conn,
        topic_id=topic_id or "",
        title=title,
        topic_url=topic_url,
        torrent_data=torrent_data,
        torrent_title=torrent_title,
        fingerprint=fingerprint,
        info_hash=info_hash,
        size_bytes=size_bytes,
        used_space=used_space,
        max_space=max_space,
    )

    torrent_already_in_qbit = find_existing_info_hash(client, info_hash)

    if torrent_already_in_qbit:
        log.info("  Такий torrent вже є в qBittorrent.")
        save_item(
            conn,
            item_key=item_key,
            topic_id=topic_id,
            title=title,
            topic_url=topic_url,
            torrent_url=torrent_url,
            fingerprint=fingerprint,
            info_hash=info_hash,
            size_bytes=size_bytes,
            status="already_in_qbit",
        )
        return used_space

    if existing:
        old_fingerprint = existing["fingerprint"]
        if old_fingerprint == fingerprint:
            # Important: an earlier DRY_RUN can have populated SQLite
            # even though qBittorrent never received the torrent. In that
            # case the DB entry alone must not block a later real run.
            if AUTO_ADD_NEW:
                projected_space = used_space + size_bytes

                if projected_space > max_space:
                    log.warning(
                        "  Відомий torrent ще не доданий у qBittorrent, "
                        "але після додавання буде перевищено ліміт."
                    )
                    save_item(
                        conn,
                        item_key=item_key,
                        topic_id=topic_id,
                        title=title,
                        topic_url=topic_url,
                        torrent_url=torrent_url,
                        fingerprint=fingerprint,
                        info_hash=info_hash,
                        size_bytes=size_bytes,
                        status="storage_limit",
                    )
                    return used_space

                log.info(
                    "  Torrent уже є в SQLite, але відсутній у qBittorrent → додаю."
                )

                try:
                    add_torrent(
                        client,
                        torrent_data,
                        torrent_title,
                    )
                except Exception as exc:
                    log.error(
                        "  Помилка додавання: %s",
                        exc,
                    )
                    save_item(
                        conn,
                        item_key=item_key,
                        topic_id=topic_id,
                        title=title,
                        topic_url=topic_url,
                        torrent_url=torrent_url,
                        fingerprint=fingerprint,
                        info_hash=info_hash,
                        size_bytes=size_bytes,
                        status="qbit_add_error",
                    )
                    return used_space

                save_item(
                    conn,
                    item_key=item_key,
                    topic_id=topic_id,
                    title=title,
                    topic_url=topic_url,
                    torrent_url=torrent_url,
                    fingerprint=fingerprint,
                    info_hash=info_hash,
                    size_bytes=size_bytes,
                    status="added",
                    last_added=now_local(),
                )

                if not DRY_RUN:
                    used_space += size_bytes

                log.info("  Готово.")
                return used_space

            log.info(
                "  Без змін. Torrent уже відомий SQLite, але не доданий у qBittorrent."
            )
            save_item(
                conn,
                item_key=item_key,
                topic_id=topic_id,
                title=title,
                topic_url=topic_url,
                torrent_url=torrent_url,
                fingerprint=fingerprint,
                info_hash=info_hash,
                size_bytes=size_bytes,
                status="known_not_added",
            )
            return used_space

        log.warning("  [UPDATE] Torrent у темі змінився!")
        save_item(
            conn,
            item_key=item_key,
            topic_id=topic_id,
            title=title,
            topic_url=topic_url,
            torrent_url=torrent_url,
            fingerprint=fingerprint,
            info_hash=info_hash,
            size_bytes=size_bytes,
            status="updated",
            last_update_seen=now_local(),
        )

        if not AUTO_UPDATE:
            log.info("  AUTO_UPDATE=false → нову версію не додаю.")
            return used_space

    projected_space = used_space + size_bytes
    if projected_space > max_space:
        log.warning("  ПРОПУСК: перевищення ліміту.")
        log.warning("  Поточне:  %s", human_size(used_space))
        log.warning("  Torrent:  %s", human_size(size_bytes))
        log.warning("  Після:    %s", human_size(projected_space))
        log.warning("  Максимум: %s", human_size(max_space))

        save_item(
            conn,
            item_key=item_key,
            topic_id=topic_id,
            title=title,
            topic_url=topic_url,
            torrent_url=torrent_url,
            fingerprint=fingerprint,
            info_hash=info_hash,
            size_bytes=size_bytes,
            status="storage_limit",
        )
        return used_space

    if existing is None and not AUTO_ADD_NEW:
        log.info("  Новий torrent знайдено, але AUTO_ADD_NEW=false.")
        save_item(
            conn,
            item_key=item_key,
            topic_id=topic_id,
            title=title,
            topic_url=topic_url,
            torrent_url=torrent_url,
            fingerprint=fingerprint,
            info_hash=info_hash,
            size_bytes=size_bytes,
            status="new_not_added",
        )
        return used_space

    log.info("  Додаю torrent у qBittorrent...")
    try:
        add_torrent(client, torrent_data, torrent_title)
    except Exception as exc:
        log.error("  Помилка додавання: %s", exc)
        save_item(
            conn,
            item_key=item_key,
            topic_id=topic_id,
            title=title,
            topic_url=topic_url,
            torrent_url=torrent_url,
            fingerprint=fingerprint,
            info_hash=info_hash,
            size_bytes=size_bytes,
            status="qbit_add_error",
        )
        return used_space

    save_item(
        conn,
        item_key=item_key,
        topic_id=topic_id,
        title=title,
        topic_url=topic_url,
        torrent_url=torrent_url,
        fingerprint=fingerprint,
        info_hash=info_hash,
        size_bytes=size_bytes,
        status="added",
        last_added=now_local(),
    )

    if not DRY_RUN:
        used_space += size_bytes

    log.info("  Готово.")
    return used_space


# ============================================================
# Feed processing
# ============================================================


def process_feed(
    client: qbittorrentapi.Client,
    conn: sqlite3.Connection,
    feed: feedparser.FeedParserDict,
) -> set[str]:
    max_space = int(TORRENT_LIMIT_GB * 1024**3)
    used_space = get_used_space(client)

    log.info(
        "Зайнято: %s / %s",
        human_size(used_space),
        human_size(max_space),
    )

    consecutive_429 = 0
    processed_topic_ids: set[str] = set()

    for entry in feed.entries:
        try:
            used_space = process_entry(
                client,
                conn,
                entry,
                used_space,
                max_space,
            )
            topic_id = extract_topic_id(entry)
            if topic_id:
                processed_topic_ids.add(topic_id)
            consecutive_429 = 0

        except TolokaRateLimitError as exc:
            consecutive_429 += 1
            log.error("Toloka rate limit: %s", exc)

            if consecutive_429 >= TOLOKA_MAX_CONSECUTIVE_429:
                log.error(
                    "Отримали кілька 429 поспіль. Завершую цей запуск, "
                    "щоб не створювати зайве навантаження на Toloka."
                )
                break

        except Exception as exc:
            log.exception(
                "Неочікувана помилка при обробці RSS item: %s",
                exc,
            )

    return processed_topic_ids


# ============================================================
# Watchlist scanning
# ============================================================


def process_watchlist(
    client: qbittorrentapi.Client,
    conn: sqlite3.Connection,
    skip_topic_ids: set[str] | None = None,
) -> None:
    """Check watched topics that were not already processed from RSS."""
    skip_topic_ids = skip_topic_ids or set()
    rows = [
        row
        for row in list_watch_items(conn)
        if row["enabled"] and row["topic_id"] not in skip_topic_ids
    ]

    if not rows:
        log.info("Watchlist: немає додаткових тем для перевірки.")
        return

    max_space = int(TORRENT_LIMIT_GB * 1024**3)
    used_space = get_used_space(client)

    log.info(
        "Watchlist: перевіряю %d тем поза поточним RSS.",
        len(rows),
    )

    consecutive_429 = 0

    for row in rows:
        topic_id = row["topic_id"]
        title = row["title"] or f"Topic {topic_id}"
        topic_url = row["topic_url"]

        log.info(
            "[WATCH] Перевіряю t%s: %s",
            topic_id,
            title,
        )

        try:
            torrent_url = resolve_torrent_url(
                topic_url,
                topic_id,
            )

            if not torrent_url:
                update_watch_item(
                    conn,
                    topic_id=topic_id,
                    title=title,
                    topic_url=topic_url,
                    status="no_torrent_url",
                )
                continue

            (
                torrent_data,
                size_bytes,
                torrent_title,
                fingerprint,
                info_hash,
            ) = download_torrent(torrent_url)

            used_space = sync_watch_item(
                client,
                conn,
                topic_id=topic_id,
                title=torrent_title or title,
                topic_url=topic_url,
                torrent_data=torrent_data,
                torrent_title=torrent_title,
                fingerprint=fingerprint,
                info_hash=info_hash,
                size_bytes=size_bytes,
                used_space=used_space,
                max_space=max_space,
            )

            consecutive_429 = 0

        except TolokaRateLimitError as exc:
            consecutive_429 += 1
            log.error("Watchlist rate limit: %s", exc)
            if consecutive_429 >= TOLOKA_MAX_CONSECUTIVE_429:
                log.error(
                    "Кілька 429 поспіль під час watchlist. " "Завершую перевірку."
                )
                break

        except Exception as exc:
            log.exception(
                "Помилка watchlist t%s: %s",
                topic_id,
                exc,
            )


# ============================================================
# CLI
# ============================================================


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Toloka Seed Manager")
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--watch-add",
        metavar="TOPIC",
        help="Додати тему tXXXXX або URL у watchlist.",
    )
    group.add_argument(
        "--watch-remove",
        metavar="TOPIC",
        help="Видалити тему tXXXXX або URL з watchlist.",
    )
    group.add_argument(
        "--watch-list",
        action="store_true",
        help="Показати watchlist і завершити.",
    )
    return parser


def handle_cli(
    conn: sqlite3.Connection,
    args: argparse.Namespace,
) -> bool:
    """Return True when the CLI action should end the program."""
    if args.watch_list:
        print_watchlist(conn)
        return True

    if args.watch_add:
        topic_id, _ = normalize_topic_input(args.watch_add)
        add_watch_item(conn, topic_id)
        print_watchlist(conn)
        return True

    if args.watch_remove:
        topic_id, _ = normalize_topic_input(args.watch_remove)
        remove_watch_item(conn, topic_id)
        print_watchlist(conn)
        return True

    return False


# ============================================================
# Main
# ============================================================


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    print()
    print("=" * 64)
    print(" Toloka Seed Manager")
    print("=" * 64)
    print()
    print(f"RSS:               {RSS_URL}")
    print(f"Save path:         {SAVE_PATH}")
    print(f"Category:          {QBIT_CATEGORY}")
    print(f"Limit:             {TORRENT_LIMIT_GB:.2f} GB")
    print(f"AUTO_ADD_NEW:      {'YES' if AUTO_ADD_NEW else 'NO'}")
    print(f"AUTO_UPDATE:       {'YES' if AUTO_UPDATE else 'NO'}")
    print(f"DRY_RUN:           {'YES' if DRY_RUN else 'NO'}")
    print(f"Toloka delay:      {TOLOKA_REQUEST_DELAY:.1f} s")
    print(f"Watchlist add:     {'YES' if WATCHLIST_AUTO_ADD else 'NO'}")
    print(f"Watchlist update:  {'YES' if WATCHLIST_AUTO_UPDATE else 'NO'}")
    print()

    SAVE_PATH.mkdir(parents=True, exist_ok=True)
    if DEBUG_SAVE_HTML:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    conn = init_database()
    client = None

    try:
        if handle_cli(conn, args):
            return 0

        client = connect_qbittorrent()
        ensure_category(client)

        # RSS itself may be public, but topic/torrent downloads require a Toloka session.
        login_to_toloka()

        feed = fetch_rss()
        processed_topic_ids = process_feed(client, conn, feed)
        process_watchlist(
            client,
            conn,
            skip_topic_ids=processed_topic_ids,
        )

        print()
        print("=" * 64)
        print(" Готово.")
        print("=" * 64)
        return 0

    except requests.HTTPError as exc:
        log.error("HTTP помилка: %s", exc)
        return 1
    except qbittorrentapi.LoginFailed as exc:
        log.error("Не вдалося увійти в qBittorrent: %s", exc)
        return 1
    except TolokaRateLimitError as exc:
        log.error("%s", exc)
        return 1
    except Exception as exc:
        log.exception("Критична помилка: %s", exc)
        return 1
    finally:
        if client is not None:
            try:
                client.auth_log_out()
            except Exception:
                pass
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
