from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable

import qbittorrentapi

from .models import TorrentMeta
from .utils import human_size, sanitize_filename

log = logging.getLogger("TolokaSeedManager.qbit")


class QBitClient:
    def __init__(self, host: str, username: str, password: str) -> None:
        self.client = qbittorrentapi.Client(
            host=host,
            username=username,
            password=password,
        )

    def login(self) -> None:
        self.client.auth_log_in()
        log.info("Підключено до qBittorrent %s", self.client.app.version)
        log.info("Web API: %s", self.client.app.web_api_version)

    def logout(self) -> None:
        try:
            self.client.auth_log_out()
        except Exception:
            pass

    def ensure_category(self, name: str, path: Path) -> None:
        categories = self.client.torrent_categories.categories
        if name in categories:
            return
        log.info("Створюю категорію qBittorrent: %s → %s", name, path)
        self.client.torrent_categories.create_category(
            name=name,
            save_path=str(path),
        )

    def ensure_tags(self, tags: Iterable[str]) -> None:
        desired = [tag for tag in tags if tag]
        if not desired:
            return
        existing = set(self.client.torrent_tags.tags)
        missing = [tag for tag in desired if tag not in existing]
        if missing:
            self.client.torrent_tags.create_tags(tags=missing)

    def list_torrents(self) -> list[object]:
        return list(self.client.torrents_info())

    def get_torrents_by_hash(self) -> dict[str, object]:
        return {str(t.hash).lower(): t for t in self.list_torrents()}

    def get_tags(self, torrent: object) -> set[str]:
        return normalize_qbit_tags(getattr(torrent, "tags", ""))

    def _is_under_base_path(self, torrent_path: str, base_path: Path) -> bool:
        if not torrent_path:
            return False
        try:
            torrent_norm = os.path.normcase(os.path.normpath(torrent_path))
            base_norm = os.path.normcase(os.path.normpath(str(base_path)))
            if torrent_norm == base_norm:
                return True
            return torrent_norm.startswith(base_norm.rstrip("\\/") + os.sep)
        except OSError:
            return False

    def managed_torrents(self, managed_tag: str, base_path: Path | None = None, ignore_tag: str | None = None) -> list[object]:
        result: list[object] = []
        for torrent in self.list_torrents():
            tags = normalize_qbit_tags(getattr(torrent, "tags", ""))
            if ignore_tag and ignore_tag in tags:
                continue
            if managed_tag in tags:
                result.append(torrent)
            elif base_path is not None and self._is_under_base_path(str(getattr(torrent, "save_path", "")), base_path):
                # Count legacy torrents in the managed folder too. This prevents
                # a v2 migration from accidentally exceeding the storage limit.
                result.append(torrent)
        return result

    def managed_size(
        self,
        managed_tag: str,
        base_path: Path | None = None,
        ignore_tag: str | None = None,
    ) -> int:
        return sum(
            int(getattr(t, "total_size", 0))
            for t in self.managed_torrents(
                managed_tag,
                base_path,
                ignore_tag,
            )
        )

    def add_torrent(self, meta: TorrentMeta, save_path: Path, managed_tag: str, dry_run: bool = False) -> bool:
        if dry_run:
            log.info(
                "[DRY RUN] Додав би torrent: %s | %s | tags=%s",
                meta.torrent_name,
                meta.category,
                ", ".join(meta.tags),
            )
            return True

        self.ensure_category(meta.category, save_path)
        self.ensure_tags([managed_tag, *meta.tags])

        filename = sanitize_filename(meta.torrent_name) + ".torrent"
        log.info("Додаю torrent у qBittorrent: %s", meta.torrent_name)
        result = self.client.torrents_add(
            torrent_files={filename: meta.data},
            save_path=str(save_path),
            category=meta.category,
            is_paused=False,
        )
        log.info("qBittorrent: %s", result)

        # Adding via Web API returns asynchronously. Use the known info hash to find it.
        torrent = self.wait_for_torrent(meta.info_hash)
        if torrent is None:
            log.warning(
                "Торрент доданий, але не з'явився у списку qBittorrent протягом очікування: %s",
                meta.info_hash,
            )
            return True

        self.apply_metadata(meta.info_hash, meta.category, meta.tags, managed_tag)
        return True

    def wait_for_torrent(self, info_hash: str, timeout_seconds: float = 10.0) -> object | None:
        import time

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            torrent = self.get_torrents_by_hash().get(info_hash.lower())
            if torrent is not None:
                return torrent
            time.sleep(0.5)
        return None

    def apply_metadata(
        self,
        info_hash: str,
        category: str,
        tags: Iterable[str],
        managed_tag: str,
    ) -> None:
        self.ensure_tags([managed_tag, *tags])
        self.client.torrents_set_category(
            category=category,
            torrent_hashes=info_hash,
        )
        self.client.torrent_tags.add_tags(
            tags=list(dict.fromkeys([managed_tag, *tags])),
            torrent_hashes=info_hash,
        )

    def delete_torrent(self, info_hash: str, delete_files: bool = False, dry_run: bool = False) -> None:
        if dry_run:
            log.info(
                "[DRY RUN] Видалив би torrent %s (files=%s)",
                info_hash,
                delete_files,
            )
            return
        self.client.torrents_delete(
            torrent_hashes=info_hash,
            delete_files=delete_files,
        )


def normalize_qbit_tags(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    try:
        return {str(item).strip() for item in value if str(item).strip()}
    except TypeError:
        return {str(value).strip()}
