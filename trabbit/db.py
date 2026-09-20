from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import ManagedTorrent
from .utils import now_iso


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.conn.close()

    def _init_schema(self) -> None:
        c = self.conn
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        c.execute(
            """
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
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist (
                topic_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                topic_url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority TEXT NOT NULL DEFAULT 'normal',
                auto_add INTEGER NOT NULL DEFAULT 0,
                auto_update INTEGER NOT NULL DEFAULT 0,
                check_interval_minutes INTEGER NOT NULL DEFAULT 1440,
                added_at TEXT NOT NULL,
                last_checked TEXT,
                next_check_at TEXT,
                last_fingerprint TEXT,
                last_info_hash TEXT,
                last_size_bytes INTEGER,
                last_status TEXT
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS managed_torrents (
                info_hash TEXT PRIMARY KEY,
                topic_id TEXT,
                title TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                category TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL,
                added_at TEXT,
                last_seen_at TEXT,
                qbit_present INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'seen'
            )
            """
        )
        c.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_managed_topic
            ON managed_torrents(topic_id)
            """
        )
        c.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_watch_next_check
            ON watchlist(enabled, next_check_at)
            """
        )
        self._migrate_legacy_columns()
        self.conn.commit()

    def _migrate_legacy_columns(self) -> None:
        def columns(table: str) -> set[str]:
            return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}

        wcols = columns("watchlist")
        required = {
            "priority": "TEXT NOT NULL DEFAULT 'normal'",
            "auto_add": "INTEGER NOT NULL DEFAULT 0",
            "auto_update": "INTEGER NOT NULL DEFAULT 0",
            "check_interval_minutes": "INTEGER NOT NULL DEFAULT 1440",
            "next_check_at": "TEXT",
        }
        for name, sql_type in required.items():
            if name not in wcols:
                self.conn.execute(f"ALTER TABLE watchlist ADD COLUMN {name} {sql_type}")

        rcols = columns("rss_items")
        for name, sql_type in {"topic_url": "TEXT", "info_hash": "TEXT"}.items():
            if name not in rcols:
                self.conn.execute(f"ALTER TABLE rss_items ADD COLUMN {name} {sql_type}")

    # ---------- RSS state ----------
    def get_rss_item(self, item_key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM rss_items WHERE item_key = ?", (item_key,)).fetchone()

    def save_rss_item(self, **kwargs: object) -> None:
        self.conn.execute(
            """
            INSERT INTO rss_items (
                item_key, topic_id, title, topic_url, torrent_url,
                fingerprint, info_hash, size_bytes, last_seen,
                last_added, last_update_seen, status
            ) VALUES (
                :item_key, :topic_id, :title, :topic_url, :torrent_url,
                :fingerprint, :info_hash, :size_bytes, :last_seen,
                :last_added, :last_update_seen, :status
            )
            ON CONFLICT(item_key) DO UPDATE SET
                topic_id=excluded.topic_id,
                title=excluded.title,
                topic_url=excluded.topic_url,
                torrent_url=excluded.torrent_url,
                fingerprint=excluded.fingerprint,
                info_hash=excluded.info_hash,
                size_bytes=excluded.size_bytes,
                last_seen=excluded.last_seen,
                last_added=COALESCE(excluded.last_added, rss_items.last_added),
                last_update_seen=COALESCE(excluded.last_update_seen, rss_items.last_update_seen),
                status=excluded.status
            """,
            {
                **kwargs,
                "last_seen": kwargs.get("last_seen") or now_iso(),
            },
        )
        self.conn.commit()

    # ---------- Managed torrent state ----------
    def get_managed(self, info_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM managed_torrents WHERE info_hash = ?", (info_hash,)
        ).fetchone()

    def get_managed_for_topic(self, topic_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM managed_torrents WHERE topic_id = ? ORDER BY added_at DESC",
            (topic_id,),
        ).fetchall())

    def upsert_managed(self, torrent: ManagedTorrent) -> None:
        self.conn.execute(
            """
            INSERT INTO managed_torrents (
                info_hash, topic_id, title, size_bytes, category,
                tags_json, source, added_at, last_seen_at,
                qbit_present, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(info_hash) DO UPDATE SET
                topic_id=excluded.topic_id,
                title=excluded.title,
                size_bytes=excluded.size_bytes,
                category=excluded.category,
                tags_json=excluded.tags_json,
                source=excluded.source,
                added_at=COALESCE(excluded.added_at, managed_torrents.added_at),
                last_seen_at=excluded.last_seen_at,
                qbit_present=excluded.qbit_present,
                status=excluded.status
            """,
            (
                torrent.info_hash,
                torrent.topic_id,
                torrent.title,
                torrent.size_bytes,
                torrent.category,
                json.dumps(torrent.tags, ensure_ascii=False),
                torrent.source,
                torrent.added_at,
                torrent.last_seen_at or now_iso(),
                int(torrent.qbit_present),
                torrent.status,
            ),
        )
        self.conn.commit()

    def all_managed(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM managed_torrents").fetchall())

    # ---------- Watchlist ----------
    def get_watch(self, topic_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM watchlist WHERE topic_id = ?", (topic_id,)).fetchone()

    def list_watch(self, enabled_only: bool = False) -> list[sqlite3.Row]:
        if enabled_only:
            return list(self.conn.execute(
                "SELECT * FROM watchlist WHERE enabled = 1 ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, added_at DESC"
            ).fetchall())
        return list(self.conn.execute(
            "SELECT * FROM watchlist ORDER BY enabled DESC, CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'normal' THEN 2 ELSE 3 END, added_at DESC"
        ).fetchall())

    def add_watch(
        self,
        topic_id: str,
        title: str,
        topic_url: str,
        priority: str = "normal",
        auto_add: bool = False,
        auto_update: bool = False,
        interval_minutes: int = 1440,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO watchlist (
                topic_id, title, topic_url, enabled, priority,
                auto_add, auto_update, check_interval_minutes, added_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(topic_id) DO UPDATE SET
                title=excluded.title,
                topic_url=excluded.topic_url,
                enabled=1,
                priority=excluded.priority,
                auto_add=excluded.auto_add,
                auto_update=excluded.auto_update,
                check_interval_minutes=excluded.check_interval_minutes
            """,
            (
                topic_id,
                title,
                topic_url,
                priority,
                int(auto_add),
                int(auto_update),
                interval_minutes,
                now_iso(),
            ),
        )
        self.conn.commit()

    def remove_watch(self, topic_id: str) -> bool:
        cur = self.conn.execute("DELETE FROM watchlist WHERE topic_id = ?", (topic_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def update_watch_state(
        self,
        topic_id: str,
        *,
        title: str | None = None,
        enabled: bool | None = None,
        priority: str | None = None,
        auto_add: bool | None = None,
        auto_update: bool | None = None,
        last_checked: str | None = None,
        next_check_at: str | None = None,
        fingerprint: str | None = None,
        info_hash: str | None = None,
        size_bytes: int | None = None,
        status: str | None = None,
    ) -> None:
        row = self.get_watch(topic_id)
        if row is None:
            raise KeyError(topic_id)
        values = {
            "title": title if title is not None else row["title"],
            "enabled": int(enabled if enabled is not None else row["enabled"]),
            "priority": priority if priority is not None else row["priority"],
            "auto_add": int(auto_add if auto_add is not None else row["auto_add"]),
            "auto_update": int(auto_update if auto_update is not None else row["auto_update"]),
            "last_checked": last_checked if last_checked is not None else row["last_checked"],
            "next_check_at": next_check_at if next_check_at is not None else row["next_check_at"],
            "last_fingerprint": fingerprint if fingerprint is not None else row["last_fingerprint"],
            "last_info_hash": info_hash if info_hash is not None else row["last_info_hash"],
            "last_size_bytes": size_bytes if size_bytes is not None else row["last_size_bytes"],
            "last_status": status if status is not None else row["last_status"],
            "topic_id": topic_id,
        }
        self.conn.execute(
            """
            UPDATE watchlist SET
                title=:title,
                enabled=:enabled,
                priority=:priority,
                auto_add=:auto_add,
                auto_update=:auto_update,
                last_checked=:last_checked,
                next_check_at=:next_check_at,
                last_fingerprint=:last_fingerprint,
                last_info_hash=:last_info_hash,
                last_size_bytes=:last_size_bytes,
                last_status=:last_status
            WHERE topic_id=:topic_id
            """,
            values,
        )
        self.conn.commit()

    # ---------- helpers ----------
    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
