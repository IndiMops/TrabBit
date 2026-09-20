from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .db import Database
from .models import Decision, ManagedTorrent, TorrentMeta
from .qbit import QBitClient
from .rules import RulesEngine
from .toloka import TolokaClient, TolokaRateLimitError
from .utils import human_size, now_iso

log = logging.getLogger("TolokaSeedManager.manager")


class Manager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        self.rules = RulesEngine(settings.rules_path)
        self.qbit = QBitClient(
            settings.qbit_host,
            settings.qbit_username,
            settings.qbit_password,
        )
        self.toloka = TolokaClient(
            settings.toloka_base_url,
            settings.toloka_login_url,
            settings.toloka_username,
            settings.toloka_password,
            settings.toloka_request_delay,
            settings.toloka_max_retries,
            settings.request_timeout,
            settings.debug_dir,
            settings.debug_save_html,
        )

    def close(self) -> None:
        self.qbit.logout()
        self.db.close()

    def login(self) -> None:
        self.qbit.login()
        # qBittorrent requires a category to exist before setCategory can be used.
        # Create every category declared in the rules up front, including categories
        # that may only be encountered for already-existing torrents.
        for category in self.rules.categories():
            self.qbit.ensure_category(
                category,
                self.rules.category_path(category, self.settings.qbit_base_path),
            )
        self.toloka.login()

    def ensure_meta_targets(self, meta: TorrentMeta) -> None:
        save_dir = self.rules.category_path(
            meta.category,
            self.settings.qbit_base_path,
        )
        self.qbit.ensure_category(meta.category, save_dir)
        self.qbit.ensure_tags([self.settings.managed_tag, *meta.tags])

    def classify(self, meta: TorrentMeta) -> None:
        meta.category, meta.tags = self.rules.classify(
            title=meta.topic_title,
            torrent_name=meta.torrent_name,
            creator=meta.creator,
            subject=meta.subject,
            watched=meta.watched,
            priority=meta.priority,
        )

    def current_managed_size(self) -> int:
        return self.qbit.managed_size(self.settings.managed_tag, self.settings.qbit_base_path, self.settings.ignore_tag)

    def _decision_for_rss(self, meta: TorrentMeta, qbit_by_hash: dict[str, object]) -> Decision:
        row = self.db.get_rss_item(f"topic:{meta.topic_id}")
        managed = self.db.get_managed(meta.info_hash)
        present = meta.info_hash.lower() in qbit_by_hash

        if present:
            return Decision("sync", "already_in_qbit", meta.category, meta.tags)

        if row is None:
            return Decision(
                "add" if self.settings.auto_add_new else "skip",
                "new_topic",
                meta.category,
                meta.tags,
            )

        old_hash = (row["info_hash"] or "").lower()
        old_fp = row["fingerprint"] or ""
        if old_hash == meta.info_hash.lower() and old_fp == meta.fingerprint:
            prior_status = str(row["status"] or "")
            pending_dry_run = prior_status.startswith("dry_run_would") or prior_status in {"new_not_added", "qbit_add_error"}
            can_readd = self.settings.auto_add_new and (self.settings.readd_missing or pending_dry_run)
            return Decision(
                "readd" if can_readd else "skip",
                "known_but_missing",
                meta.category,
                meta.tags,
            )

        return Decision(
            "add_update" if self.settings.auto_update else "skip_update",
            "updated_topic",
            meta.category,
            meta.tags,
        )

    def _fits_storage(self, size_bytes: int, used_bytes: int) -> bool:
        return used_bytes + size_bytes <= self.settings.max_bytes()

    def add_meta(
        self,
        meta: TorrentMeta,
        *,
        source: str,
        action: str,
        qbit_by_hash: dict[str, object],
        used_bytes: int,
    ) -> int:
        meta.category, meta.tags = self.rules.classify(
            title=meta.topic_title,
            torrent_name=meta.torrent_name,
            creator=meta.creator,
            subject=meta.subject,
            watched=meta.watched,
            priority=meta.priority,
        )
        save_dir = self.rules.category_path(meta.category, self.settings.qbit_base_path)

        if meta.info_hash.lower() in qbit_by_hash:
            self.db.upsert_managed(ManagedTorrent(
                info_hash=meta.info_hash,
                topic_id=meta.topic_id,
                title=meta.torrent_name,
                size_bytes=meta.size_bytes,
                category=meta.category,
                tags=meta.tags,
                source=source,
                last_seen_at=now_iso(),
                qbit_present=True,
                status="present",
            ))
            if not self.settings.dry_run:
                self.ensure_meta_targets(meta)
                self.qbit.apply_metadata(meta.info_hash, meta.category, meta.tags, self.settings.managed_tag)
            return used_bytes

        if not self._fits_storage(meta.size_bytes, used_bytes):
            log.warning(
                "  Ліміт сховища: %s + %s > %s. Пропускаю.",
                human_size(used_bytes),
                human_size(meta.size_bytes),
                human_size(self.settings.max_bytes()),
            )
            self.db.upsert_managed(ManagedTorrent(
                info_hash=meta.info_hash,
                topic_id=meta.topic_id,
                title=meta.torrent_name,
                size_bytes=meta.size_bytes,
                category=meta.category,
                tags=meta.tags,
                source=source,
                last_seen_at=now_iso(),
                qbit_present=False,
                status="storage_limit",
            ))
            return used_bytes

        log.info(
            "  Category: %s | Tags: %s",
            meta.category,
            ", ".join(meta.tags),
        )
        self.qbit.add_torrent(
            meta,
            save_dir,
            self.settings.managed_tag,
            dry_run=self.settings.dry_run,
        )

        if self.settings.dry_run:
            status = "dry_run_would_add"
            qbit_present = False
        else:
            status = "added"
            qbit_present = True
            used_bytes += meta.size_bytes
            qbit_by_hash[meta.info_hash.lower()] = object()

        self.db.upsert_managed(ManagedTorrent(
            info_hash=meta.info_hash,
            topic_id=meta.topic_id,
            title=meta.torrent_name,
            size_bytes=meta.size_bytes,
            category=meta.category,
            tags=meta.tags,
            source=source,
            added_at=now_iso() if not self.settings.dry_run else None,
            last_seen_at=now_iso(),
            qbit_present=qbit_present,
            status=status,
        ))
        return used_bytes

    def process_rss(self) -> set[str]:
        feed = self.toloka.fetch_rss(self.settings.rss_url)
        qbit_by_hash = self.qbit.get_torrents_by_hash()
        used_bytes = self.current_managed_size()
        processed: set[str] = set()

        log.info(
            "Зайнято: %s / %s",
            human_size(used_bytes),
            human_size(self.settings.max_bytes()),
        )
        if used_bytes >= self.settings.warning_bytes():
            log.warning(
                "Сховище наближається до ліміту: %s / %s",
                human_size(used_bytes),
                human_size(self.settings.max_bytes()),
            )

        consecutive_429 = 0
        for entry in feed.entries:
            topic_id, topic_url, title, creator, subject = self.toloka.parse_topic(entry)
            if not topic_id or not topic_url:
                continue
            processed.add(topic_id)
            watched_row = self.db.get_watch(topic_id)
            priority = watched_row["priority"] if watched_row else "normal"
            watched = watched_row is not None and bool(watched_row["enabled"])
            log.info("Перевіряю: %s", title)
            try:
                meta = self.toloka.fetch_meta(
                    topic_id,
                    topic_url,
                    title,
                    creator,
                    subject,
                    watched=watched,
                    priority=priority,
                )
                if meta is None:
                    continue

                decision = self._decision_for_rss(meta, qbit_by_hash)
                log.info(
                    "  Decision: %s (%s)",
                    decision.action,
                    decision.reason,
                )

                # Always persist what we actually saw. Do not mark it as added during dry-run.
                existing = self.db.get_rss_item(f"topic:{topic_id}")
                status = "seen"
                last_added = None
                last_update = None
                if decision.action in {"add", "readd", "add_update"}:
                    used_bytes = self.add_meta(
                        meta,
                        source="rss",
                        action=decision.action,
                        qbit_by_hash=qbit_by_hash,
                        used_bytes=used_bytes,
                    )
                    status = "added" if not self.settings.dry_run else "dry_run_would_add"
                    last_added = now_iso() if not self.settings.dry_run else None
                    if existing and decision.reason == "updated_topic":
                        last_update = now_iso()
                elif decision.action == "sync":
                    self.ensure_meta_targets(meta)
                    self.qbit.apply_metadata(meta.info_hash, meta.category, meta.tags, self.settings.managed_tag)
                    self.db.upsert_managed(ManagedTorrent(
                        info_hash=meta.info_hash,
                        topic_id=meta.topic_id,
                        title=meta.torrent_name,
                        size_bytes=meta.size_bytes,
                        category=meta.category,
                        tags=meta.tags,
                        source="rss",
                        added_at=None,
                        last_seen_at=now_iso(),
                        qbit_present=True,
                        status="present",
                    ))
                    status = "present"
                else:
                    status = decision.reason

                self.db.save_rss_item(
                    item_key=f"topic:{topic_id}",
                    topic_id=topic_id,
                    title=title,
                    topic_url=topic_url,
                    torrent_url=meta.torrent_url,
                    fingerprint=meta.fingerprint,
                    info_hash=meta.info_hash,
                    size_bytes=meta.size_bytes,
                    last_added=last_added,
                    last_update_seen=last_update,
                    status=status,
                )
                consecutive_429 = 0

            except TolokaRateLimitError:
                consecutive_429 += 1
                if consecutive_429 >= self.settings.toloka_max_consecutive_429:
                    log.error("Кілька 429 поспіль. RSS-сканування зупинено.")
                    break
            except Exception:
                log.exception("Помилка обробки t%s", topic_id)

        return processed

    def _watch_should_check(self, row: object) -> bool:
        # next_check_at is ISO UTC. If absent, check now.
        value = row["next_check_at"]
        if not value:
            return True
        try:
            next_check = datetime.fromisoformat(value)
        except ValueError:
            return True
        return datetime.now(timezone.utc) >= next_check

    def _next_check(self, minutes: int) -> str:
        return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()

    def process_watchlist(self, skip_topics: set[str]) -> None:
        rows = [row for row in self.db.list_watch(enabled_only=True) if row["topic_id"] not in skip_topics and self._watch_should_check(row)]
        if not rows:
            log.info("Watchlist: немає додаткових тем для перевірки.")
            return

        qbit_by_hash = self.qbit.get_torrents_by_hash()
        used_bytes = self.current_managed_size()
        log.info("Watchlist: перевіряю %d тем поза поточним RSS.", len(rows))

        for row in rows:
            topic_id = row["topic_id"]
            title = row["title"]
            try:
                log.info("[WATCH] Перевіряю t%s: %s", topic_id, title)
                meta = self.toloka.fetch_meta(
                    topic_id,
                    row["topic_url"],
                    title,
                    watched=True,
                    priority=row["priority"],
                )
                now = now_iso()
                next_check = self._next_check(int(row["check_interval_minutes"]))
                if meta is None:
                    self.db.update_watch_state(
                        topic_id,
                        last_checked=now,
                        next_check_at=next_check,
                        status="no_torrent_url",
                    )
                    continue

                meta.watched = True
                meta.priority = row["priority"]
                self.classify(meta)

                old_fp = row["last_fingerprint"] or ""
                old_hash = row["last_info_hash"] or ""
                present = meta.info_hash.lower() in qbit_by_hash

                if not old_fp:
                    changed = False
                    reason = "baseline"
                else:
                    changed = old_fp != meta.fingerprint or old_hash.lower() != meta.info_hash.lower()
                    reason = "updated" if changed else "unchanged"

                if present:
                    self.ensure_meta_targets(meta)
                    self.qbit.apply_metadata(meta.info_hash, meta.category, meta.tags, self.settings.managed_tag)
                    self.db.upsert_managed(ManagedTorrent(
                        info_hash=meta.info_hash,
                        topic_id=topic_id,
                        title=meta.torrent_name,
                        size_bytes=meta.size_bytes,
                        category=meta.category,
                        tags=meta.tags,
                        source="watchlist",
                        last_seen_at=now,
                        qbit_present=True,
                        status="present",
                    ))
                    status = "present"
                elif (not old_fp and (bool(row["auto_add"]) or self.settings.watchlist_auto_add)):
                    used_bytes = self.add_meta(
                        meta,
                        source="watchlist",
                        action="watch_add",
                        qbit_by_hash=qbit_by_hash,
                        used_bytes=used_bytes,
                    )
                    status = "added" if not self.settings.dry_run else "dry_run_would_add"
                elif changed and (bool(row["auto_update"]) or self.settings.watchlist_auto_update):
                    used_bytes = self.add_meta(
                        meta,
                        source="watchlist_update",
                        action="watch_update",
                        qbit_by_hash=qbit_by_hash,
                        used_bytes=used_bytes,
                    )
                    status = "updated" if not self.settings.dry_run else "dry_run_would_update"
                else:
                    status = reason

                self.db.update_watch_state(
                    topic_id,
                    title=meta.torrent_name or title,
                    last_checked=now,
                    next_check_at=next_check,
                    fingerprint=meta.fingerprint,
                    info_hash=meta.info_hash,
                    size_bytes=meta.size_bytes,
                    status=status,
                )
                log.info("  [WATCH] %s", status)

            except TolokaRateLimitError:
                log.error("Watchlist: Toloka rate-limit для t%s", topic_id)
                break
            except Exception:
                log.exception("Watchlist: помилка t%s", topic_id)

    def add_watch(self, topic_input: str, priority: str = "normal", auto_add: bool | None = None, auto_update: bool | None = None, interval_minutes: int = 1440) -> None:
        import re
        match = re.search(r"/t(\d+)|^t?(\d+)$", topic_input.strip(), re.IGNORECASE)
        if not match:
            raise ValueError("Вкажи tXXXXX або URL теми Toloka.")
        topic_id = match.group(1) or match.group(2)
        topic_url = f"{self.settings.toloka_base_url}/t{topic_id}"
        self.toloka.login()
        response = self.toloka._get(topic_url, headers={"Referer": self.settings.toloka_base_url + "/"})
        response.raise_for_status()
        from bs4 import BeautifulSoup
        title_node = BeautifulSoup(response.text, "html.parser").find("title")
        title = title_node.get_text(" ", strip=True) if title_node else f"Topic {topic_id}"
        self.db.add_watch(
            topic_id,
            title,
            topic_url,
            priority=priority,
            auto_add=self.settings.watchlist_auto_add if auto_add is None else auto_add,
            auto_update=self.settings.watchlist_auto_update if auto_update is None else auto_update,
            interval_minutes=interval_minutes,
        )
        log.info("Watchlist: додано t%s | %s", topic_id, title)

    def remove_watch(self, topic_input: str) -> None:
        import re
        match = re.search(r"/t(\d+)|^t?(\d+)$", topic_input.strip(), re.IGNORECASE)
        if not match:
            raise ValueError("Вкажи tXXXXX або URL теми Toloka.")
        topic_id = match.group(1) or match.group(2)
        if self.db.remove_watch(topic_id):
            log.info("Watchlist: видалено t%s", topic_id)
        else:
            log.warning("Watchlist: t%s не знайдено", topic_id)

    def print_watchlist(self) -> None:
        rows = self.db.list_watch()
        print("\nWatchlist")
        print("=" * 90)
        if not rows:
            print("(порожньо)")
            return
        for row in rows:
            print(
                f"t{row['topic_id']} | {'ON ' if row['enabled'] else 'OFF'} | "
                f"priority={row['priority']:<8} | auto_add={bool(row['auto_add'])} | "
                f"auto_update={bool(row['auto_update'])} | {row['title']}"
            )

    def retag_existing(self) -> None:
        qbit_torrents = self.qbit.list_torrents()
        log.info("Retag: знайдено %d торрентів у qBittorrent.", len(qbit_torrents))

        managed_count = 0
        ignored_count = 0
        skipped_count = 0

        for torrent in qbit_torrents:
            info_hash = str(torrent.hash).lower()
            managed = self.db.get_managed(info_hash)
            title = str(getattr(torrent, "name", "torrent"))
            current_category = str(getattr(torrent, "category", "") or "").strip()
            current_tags = self.qbit.get_tags(torrent)

            # An explicit ignore tag always wins. This protects unrelated torrents
            # even when they happen to live in the old TolokaSeed category.
            if self.settings.ignore_tag in current_tags:
                ignored_count += 1
                log.info(
                    "[IGNORED] %s | tag=%s",
                    title,
                    self.settings.ignore_tag,
                )
                continue

            # A torrent is considered managed if it was recorded in our DB,
            # already has the managed tag, or belongs to the legacy TolokaSeed
            # category from pre-v2 installations. Unrelated torrents are skipped.
            is_managed = (
                managed is not None
                or self.settings.managed_tag in current_tags
                or current_category in self.settings.legacy_managed_categories
            )

            if not is_managed:
                skipped_count += 1
                log.info(
                    "[SKIP] %s | category=%s | причина: torrent не належить TrabBit",
                    title,
                    current_category or "(none)",
                )
                continue

            managed_count += 1
            topic_id = managed["topic_id"] if managed else None

            if managed:
                category = managed["category"]
                tags = __import__("json").loads(managed["tags_json"] or "[]")
            else:
                fake = TorrentMeta(
                    topic_id=topic_id or "",
                    topic_url="",
                    topic_title=title,
                    torrent_url="",
                    torrent_name=title,
                    data=b"",
                    size_bytes=int(getattr(torrent, "total_size", 0)),
                    fingerprint="",
                    info_hash=info_hash,
                )
                self.classify(fake)
                category, tags = fake.category, fake.tags

            tags = list(dict.fromkeys([self.settings.managed_tag, *tags]))

            if self.settings.dry_run:
                log.info(
                    "[DRY RUN] Retag: %s → %s | %s",
                    title,
                    category,
                    ", ".join(tags),
                )
                continue

            save_dir = self.rules.category_path(
                category,
                self.settings.qbit_base_path,
            )
            self.qbit.ensure_category(
                category,
                save_dir,
            )
            self.qbit.ensure_tags(tags)
            self.qbit.apply_metadata(
                info_hash,
                category,
                tags,
                self.settings.managed_tag,
            )

        log.info(
            "Retag: завершено. managed=%d, ignored=%d, skipped=%d.",
            managed_count,
            ignored_count,
            skipped_count,
        )
