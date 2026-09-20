from __future__ import annotations

import logging
import json
from dataclasses import dataclass
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Settings
from .db import Database
from .models import Decision, ManagedTorrent, RetentionCandidate, TorrentMeta
from .qbit import QBitClient
from .rules import RulesEngine
from .toloka import TolokaClient, TolokaRateLimitError
from .utils import human_size, now_iso

log = logging.getLogger("TolokaSeedManager.manager")


@dataclass(frozen=True)
class CycleResult:
    processed_topics: set[str]
    updated_topics: int
    retention_candidates: list[RetentionCandidate]
    used_bytes: int


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
            topic_details=meta.topic_details,
        )

    def current_managed_size(self) -> int:
        return self.qbit.managed_size(
            self.settings.managed_tag,
            self.settings.qbit_base_path,
            self.settings.ignore_tag,
            self.settings.legacy_managed_categories,
        )

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
            topic_details=meta.topic_details,
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

    def process_watchlist(self, skip_topics: set[str], *, force: bool = False) -> None:
        """Check enabled watchlist topics.

        In a full maintenance cycle watchlist topics are pinned and must be
        checked every cycle, so ``run_cycle()`` calls this with ``force=True``.
        The stored interval is still respected by callers that invoke this
        method without force.
        """
        all_rows = self.db.list_watch(enabled_only=True)
        if force:
            rows = [row for row in all_rows if row["topic_id"] not in skip_topics]
        else:
            rows = [
                row for row in all_rows
                if row["topic_id"] not in skip_topics and self._watch_should_check(row)
            ]

        if not rows:
            if force:
                log.info("Watchlist: активних тем для перевірки немає.")
            else:
                log.info("Watchlist: немає тем, у яких настав час перевірки.")
            return

        qbit_by_hash = self.qbit.get_torrents_by_hash()
        used_bytes = self.current_managed_size()
        log.info("Watchlist: перевіряю %d тем%s.", len(rows), " (примусово)" if force else "")

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

    def process_managed_updates(self, skip_topics: set[str]) -> int:
        """Check older managed topics for a newer torrent revision.

        Watchlist topics are excluded because they are handled by
        ``process_watchlist``. Newer torrent hashes are added when AUTO_UPDATE is
        enabled. Older hashes are only marked as ``superseded``; deletion is a
        separate user-confirmed retention action.
        """
        topics: dict[str, object] = {}
        for row in self.db.all_managed():
            topic_id = str(row["topic_id"] or "").strip()
            if not topic_id or topic_id in skip_topics:
                continue
            if topic_id not in topics:
                topics[topic_id] = row

        if not topics:
            log.info("Оновлення старих роздач: немає тем для перевірки.")
            return 0

        qbit_by_hash = self.qbit.get_torrents_by_hash()
        used_bytes = self.current_managed_size()
        updated = 0

        log.info("Оновлення старих роздач: перевіряю %d тем.", len(topics))

        for topic_id, row in topics.items():
            title = str(row["title"] or f"t{topic_id}")
            topic_url = f"{self.settings.toloka_base_url}/t{topic_id}"
            try:
                log.info("[UPDATE] Перевіряю t%s: %s", topic_id, title)
                meta = self.toloka.fetch_meta(topic_id, topic_url, title)
                if meta is None:
                    log.info("  [UPDATE] t%s: torrent URL не знайдений.", topic_id)
                    continue

                self.classify(meta)
                meta_hash = meta.info_hash.lower()
                present = meta_hash in qbit_by_hash

                if present:
                    self.ensure_meta_targets(meta)
                    if not self.settings.dry_run:
                        self.qbit.apply_metadata(
                            meta.info_hash,
                            meta.category,
                            meta.tags,
                            self.settings.managed_tag,
                        )
                        self.db.mark_topic_superseded(topic_id, meta_hash)
                    self.db.upsert_managed(ManagedTorrent(
                        info_hash=meta.info_hash,
                        topic_id=topic_id,
                        title=meta.torrent_name,
                        size_bytes=meta.size_bytes,
                        category=meta.category,
                        tags=meta.tags,
                        source="managed-update",
                        last_seen_at=now_iso(),
                        qbit_present=True,
                        status="present",
                    ))
                    continue

                previous_hashes = {
                    str(item["info_hash"]).lower()
                    for item in self.db.get_managed_for_topic(topic_id)
                }
                is_new_revision = meta_hash not in previous_hashes

                if is_new_revision and self.settings.auto_update:
                    before = used_bytes
                    used_bytes = self.add_meta(
                        meta,
                        source="managed-update",
                        action="update_old_topic",
                        qbit_by_hash=qbit_by_hash,
                        used_bytes=used_bytes,
                    )
                    if used_bytes > before:
                        updated += 1
                        if not self.settings.dry_run:
                            self.db.mark_topic_superseded(topic_id, meta_hash)
                        log.info("  [UPDATE] t%s: додано нову ревізію.", topic_id)
                else:
                    log.info("  [UPDATE] t%s: без змін.", topic_id)

            except TolokaRateLimitError:
                log.error("Оновлення старих роздач: rate-limit на t%s. Зупиняю скан.", topic_id)
                break
            except Exception:
                log.exception("Оновлення старої теми: помилка t%s", topic_id)

        log.info("Оновлення старих роздач: оновлено=%d.", updated)
        return updated

    @staticmethod
    def _retention_score(torrent: object, *, superseded: bool = False) -> tuple[float, str]:
        now_ts = int(datetime.now(timezone.utc).timestamp())
        added_on = int(getattr(torrent, "added_on", 0) or 0)
        last_activity = int(getattr(torrent, "last_activity", 0) or 0)
        age_days = max(0.0, (now_ts - added_on) / 86400.0) if added_on else 9999.0
        inactive_days = max(0.0, (now_ts - last_activity) / 86400.0) if last_activity else 9999.0
        ratio = float(getattr(torrent, "ratio", 0.0) or 0.0)
        popularity = float(getattr(torrent, "popularity", 0.0) or 0.0)
        num_seeds = int(getattr(torrent, "num_seeds", 0) or 0)
        num_leechs = int(getattr(torrent, "num_leechs", 0) or 0)
        availability = float(getattr(torrent, "availability", 0.0) or 0.0)

        score = 100.0
        reasons: list[str] = []

        if age_days > 180:
            score -= 20
            reasons.append(f"вік {age_days:.0f} дн.")
        elif age_days > 90:
            score -= 12
            reasons.append(f"вік {age_days:.0f} дн.")
        elif age_days > 30:
            score -= 6
            reasons.append(f"вік {age_days:.0f} дн.")

        if inactive_days > 90:
            score -= 25
            reasons.append(f"без активності {inactive_days:.0f} дн.")
        elif inactive_days > 30:
            score -= 16
            reasons.append(f"без активності {inactive_days:.0f} дн.")
        elif inactive_days > 14:
            score -= 8
            reasons.append(f"без активності {inactive_days:.0f} дн.")

        if ratio < 0.25:
            score -= 20
            reasons.append(f"ratio {ratio:.2f}")
        elif ratio < 0.5:
            score -= 12
            reasons.append(f"ratio {ratio:.2f}")
        elif ratio < 1.0:
            score -= 6
            reasons.append(f"ratio {ratio:.2f}")

        if num_leechs > 0:
            score += min(25, 10 + num_leechs * 5)
        elif num_seeds == 0:
            score -= 6
            reasons.append("зараз ніхто не качає")

        # Popularity is deliberately a small part of the score because qBittorrent
        # defines it from Ratio / Time Active (months), not as current peer demand.
        if popularity > 20:
            score += 8
        elif popularity > 5:
            score += 4
        elif popularity < 0.5:
            score -= 4

        # Rare content deserves protection even when it is currently quiet.
        if availability < 1.0 and num_seeds <= 1:
            score += 18
            reasons.append("рідкісний/низька доступність")

        if superseded:
            score -= 45
            reasons.insert(0, "замінений новішою ревізією теми")

        score = max(0.0, min(100.0, score))
        if not reasons:
            reasons.append("низька активність")
        return score, "; ".join(reasons)

    def scan_retention_candidates(self) -> list[RetentionCandidate]:
        if not self.settings.retention_scan_enabled:
            return []

        used_bytes = self.current_managed_size()
        if used_bytes < self.settings.warning_bytes():
            log.info(
                "Retention: %s / %s, поріг %s ще не досягнуто.",
                human_size(used_bytes),
                human_size(self.settings.max_bytes()),
                human_size(self.settings.warning_bytes()),
            )
            return []

        watch_topics = {str(row["topic_id"]) for row in self.db.list_watch(enabled_only=True)}
        db_by_hash = {str(row["info_hash"]).lower(): row for row in self.db.all_managed()}
        qbit_torrents = self.qbit.managed_torrents(
            self.settings.managed_tag,
            self.settings.qbit_base_path,
            self.settings.ignore_tag,
            self.settings.legacy_managed_categories,
        )

        candidates: list[RetentionCandidate] = []
        now_ts = int(datetime.now(timezone.utc).timestamp())
        for torrent in qbit_torrents:
            info_hash = str(getattr(torrent, "hash", "")).lower()
            db_row = db_by_hash.get(info_hash)
            topic_id = str(db_row["topic_id"]) if db_row and db_row["topic_id"] else None

            if topic_id and topic_id in watch_topics:
                continue

            added_on = int(getattr(torrent, "added_on", 0) or 0)
            last_activity = int(getattr(torrent, "last_activity", 0) or 0)
            age_days = (now_ts - added_on) / 86400.0 if added_on else 9999.0
            superseded = bool(db_row and str(db_row["status"] or "") == "superseded")

            ratio = float(getattr(torrent, "ratio", 0.0) or 0.0)
            num_leechs = int(getattr(torrent, "num_leechs", 0) or 0)

            # Community rule: never stop seeding before at least 15% has been
            # uploaded. Keep this as a hard retention guard, not a score penalty.
            if ratio < self.settings.min_seed_ratio:
                log.info(
                    "Retention: пропускаю %s — ratio %.2f < мінімум %.2f.",
                    str(getattr(torrent, "name", "torrent")),
                    ratio,
                    self.settings.min_seed_ratio,
                )
                continue

            # Protect torrents with active downloaders. Even if the retention
            # score is otherwise low, we should not remove a torrent while someone
            # is actively downloading from us.
            if self.settings.protect_active_leechers and num_leechs > 0:
                log.info(
                    "Retention: пропускаю %s — активних leechers=%d.",
                    str(getattr(torrent, "name", "torrent")),
                    num_leechs,
                )
                continue

            # Ordinary retention candidates must be old enough. Superseded versions
            # may be offered immediately because a newer revision already exists.
            if not superseded and age_days < self.settings.retention_min_age_days:
                continue

            score, reason = self._retention_score(torrent, superseded=superseded)
            if score > self.settings.retention_score_threshold and not superseded:
                continue

            candidates.append(RetentionCandidate(
                info_hash=info_hash,
                topic_id=topic_id,
                title=str(getattr(torrent, "name", "torrent")),
                size_bytes=int(getattr(torrent, "total_size", 0) or 0),
                score=score,
                reason=reason,
                ratio=float(getattr(torrent, "ratio", 0.0) or 0.0),
                popularity=float(getattr(torrent, "popularity", 0.0) or 0.0),
                num_seeds=int(getattr(torrent, "num_seeds", 0) or 0),
                num_leechs=int(getattr(torrent, "num_leechs", 0) or 0),
                added_on=added_on,
                last_activity=last_activity,
                superseded=superseded,
                # Never delete files for superseded revisions by default. The old and
                # new torrents can point to overlapping data in the same save path.
                delete_files=not superseded,
            ))

        candidates.sort(key=lambda item: (item.score, -item.size_bytes))
        log.info("Retention: знайдено %d кандидатів.", len(candidates))
        return candidates

    def delete_retention_candidate(self, candidate: RetentionCandidate) -> None:
        self.qbit.delete_torrent(
            candidate.info_hash,
            delete_files=candidate.delete_files,
            dry_run=self.settings.dry_run,
        )
        if not self.settings.dry_run:
            self.db.mark_managed_deleted(candidate.info_hash)
        log.info(
            "Retention: видалено %s | files=%s | score=%.1f | %s",
            candidate.title,
            candidate.delete_files,
            candidate.score,
            candidate.reason,
        )

    def run_cycle(self, *, include_watchlist: bool = True) -> CycleResult:
        """Run one full maintenance cycle in the canonical order used by CLI and tray.

        Order:
        1) watchlist topics
        2) updates of older managed topics (excluding active watchlist topics)
        3) retention candidate scan
        4) fresh RSS discovery
        """
        watch_topics = {
            str(row["topic_id"])
            for row in self.db.list_watch(enabled_only=True)
            if row["topic_id"]
        }

        log.info("=== Цикл TrabBit: 1/4 Watchlist ===")
        if include_watchlist:
            self.process_watchlist(set(), force=True)
        else:
            log.info("Watchlist: пропущено параметром запуску.")

        log.info("=== Цикл TrabBit: 2/4 Оновлення старих тем ===")
        updated = self.process_managed_updates(watch_topics)

        log.info("=== Цикл TrabBit: 3/4 Retention ===")
        candidates = self.scan_retention_candidates()

        log.info("=== Цикл TrabBit: 4/4 RSS discovery ===")
        processed = self.process_rss()

        used_bytes = self.current_managed_size()
        log.info(
            "=== Цикл TrabBit завершено: watchlist=%s, updated=%d, retention=%d, RSS=%d, зайнято=%s ===",
            "ON" if include_watchlist else "OFF",
            updated,
            len(candidates),
            len(processed),
            human_size(used_bytes),
        )
        return CycleResult(
            processed_topics=processed,
            updated_topics=updated,
            retention_candidates=candidates,
            used_bytes=used_bytes,
        )

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

    def analyze_topic(self, topic_input: str) -> None:
        import re

        match = re.search(r"/t(\d+)|^t?(\d+)$", topic_input.strip(), re.IGNORECASE)
        if not match:
            raise ValueError("Вкажи tXXXXX або URL теми Toloka.")

        topic_id = match.group(1) or match.group(2)
        topic_url = f"{self.settings.toloka_base_url}/t{topic_id}"
        torrent_url, details = self.toloka.fetch_topic_page(topic_url, topic_id)
        if not torrent_url:
            raise RuntimeError(f"Не знайдено torrent URL у t{topic_id}.")

        # Downloading the torrent here is intentional: the command is an analysis/debug
        # command and shows both page-derived and torrent-derived metadata without adding it.
        data, torrent_name, size, fingerprint, info_hash = self.toloka.download_torrent(torrent_url)
        meta = TorrentMeta(
            topic_id=topic_id,
            topic_url=topic_url,
            topic_title=torrent_name,
            torrent_url=torrent_url,
            torrent_name=torrent_name,
            data=data,
            size_bytes=size,
            fingerprint=fingerprint,
            info_hash=info_hash,
            topic_details=details,
        )
        self.classify(meta)

        print("\nTopic analysis")
        print("=" * 80)
        print(f"Topic:        t{topic_id}")
        print(f"Torrent:      {torrent_name}")
        print(f"Size:         {human_size(size)}")
        print(f"Info hash:    {info_hash}")
        print(f"Torrent URL:  {torrent_url}")
        print()
        print(f"18+:          {details.age_restricted}")
        print(f"Genres:       {', '.join(details.genres) or '-'}")
        print(f"Country:      {details.country or '-'}")
        print(f"Studio:       {details.studio or '-'}")
        print(f"Director:     {details.director or '-'}")
        print(f"Quality:      {details.quality or '-'}")
        print(f"Video codec:  {details.video_codec or '-'}")
        print(f"Resolution:   {details.video_width or '?'}x{details.video_height or '?'}")
        print(f"Audio lang:   {', '.join(details.audio_languages) or '-'}")
        print(f"Audio trans:  {', '.join(details.audio_translations) or '-'}")
        print(f"Sub lang:     {', '.join(details.subtitle_languages) or '-'}")
        print(f"Sub formats:  {', '.join(details.subtitle_formats) or '-'}")
        print(f"Source:       {details.source or '-'}")
        print(f"Translator:   {details.translator or '-'}")
        print()
        print(f"Category:     {meta.category}")
        print(f"Tags:         {', '.join(meta.tags)}")
        print("=" * 80)

    def retag_existing(self, *, deep: bool = False) -> None:
        qbit_torrents = self.qbit.list_torrents()
        log.info("Retag: знайдено %d торрентів у qBittorrent.%s", len(qbit_torrents), " Deep mode." if deep else "")

        managed_count = ignored_count = skipped_count = deep_skipped = 0
        batch_count = 0
        removable_static = self.rules.managed_tag_names(self.settings.managed_tag)

        for torrent in qbit_torrents:
            info_hash = str(torrent.hash).lower()
            managed = self.db.get_managed(info_hash)
            title = str(getattr(torrent, "name", "torrent"))
            current_category = str(getattr(torrent, "category", "") or "").strip()
            current_tags = self.qbit.get_tags(torrent)

            if self.settings.ignore_tag in current_tags:
                ignored_count += 1
                log.info("[IGNORED] %s | tag=%s", title, self.settings.ignore_tag)
                continue

            is_managed = (
                managed is not None
                or self.settings.managed_tag in current_tags
                or current_category in self.settings.legacy_managed_categories
            )
            if not is_managed:
                skipped_count += 1
                log.info("[SKIP] %s | category=%s | причина: torrent не належить TrabBit", title, current_category or "(none)")
                continue

            managed_count += 1
            topic_id = managed["topic_id"] if managed else None
            details = None

            if deep:
                if not topic_id:
                    deep_skipped += 1
                    log.warning("[SKIP DEEP] %s | немає topic_id у SQLite", title)
                    continue

                topic_url = f"{self.settings.toloka_base_url}/t{topic_id}"
                watch = self.db.get_watch(str(topic_id))
                try:
                    _, details = self.toloka.fetch_topic_page(topic_url, str(topic_id))
                except TolokaRateLimitError:
                    log.error("Deep retag: Toloka rate-limit на t%s. Зупиняю deep scan.", topic_id)
                    break
                except Exception:
                    log.exception("Deep retag: помилка читання t%s", topic_id)
                    continue

                meta = TorrentMeta(
                    topic_id=str(topic_id),
                    topic_url=topic_url,
                    topic_title=title,
                    torrent_url="",
                    torrent_name=title,
                    data=b"",
                    size_bytes=int(getattr(torrent, "total_size", 0)),
                    fingerprint="",
                    info_hash=info_hash,
                    watched=bool(watch and watch["enabled"]),
                    priority=str(watch["priority"] if watch else "normal"),
                    topic_details=details,
                )
                self.classify(meta)
                category, desired_tags = meta.category, list(dict.fromkeys([self.settings.managed_tag, *meta.tags]))
                log.info("[DEEP] %s → %s | %s", title, category, ", ".join(desired_tags))
            else:
                if managed:
                    category = managed["category"]
                    tags = json.loads(managed["tags_json"] or "[]")
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
                desired_tags = list(dict.fromkeys([self.settings.managed_tag, *tags]))

            removable = set(removable_static) | {tag for tag in current_tags if self.rules.is_managed_dynamic_tag(tag)}
            to_add = set(desired_tags) - current_tags
            to_remove = {tag for tag in current_tags if tag in removable and tag not in desired_tags}

            if self.settings.dry_run:
                log.info("[DRY RUN] Retag: %s → %s | +%s | -%s", title, category, ", ".join(sorted(to_add)) or "-", ", ".join(sorted(to_remove)) or "-")
            else:
                save_dir = self.rules.category_path(category, self.settings.qbit_base_path)
                self.qbit.ensure_category(category, save_dir)
                self.qbit.sync_metadata(
                    info_hash,
                    category,
                    desired_tags,
                    self.settings.managed_tag,
                    removable,
                    dry_run=False,
                )
                if managed:
                    self.db.upsert_managed(ManagedTorrent(
                        info_hash=info_hash,
                        topic_id=managed["topic_id"],
                        title=title,
                        size_bytes=int(getattr(torrent, "total_size", 0)),
                        category=category,
                        tags=desired_tags,
                        source=managed["source"],
                        added_at=managed["added_at"],
                        last_seen_at=now_iso(),
                        qbit_present=True,
                        status="retagged_deep" if deep else "retagged",
                    ))

            batch_count += 1
            if deep and batch_count % self.settings.deep_retag_batch_size == 0 and self.settings.deep_retag_batch_pause > 0:
                log.info("Deep retag: оброблено %d, пауза %.1f с для Toloka.", batch_count, self.settings.deep_retag_batch_pause)
                time.sleep(self.settings.deep_retag_batch_pause)

        log.info("Retag: завершено. managed=%d, ignored=%d, skipped=%d, deep_skipped=%d.", managed_count, ignored_count, skipped_count, deep_skipped)

