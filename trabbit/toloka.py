from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup

from .models import TopicDetails, TorrentMeta
from .torrent import parse_torrent
from .topic_parser import build_detail_tags, parse_topic_page
from .utils import ensure_dir

log = logging.getLogger("TolokaSeedManager.toloka")


class TolokaRateLimitError(RuntimeError):
    pass


class TolokaClient:
    def __init__(
        self,
        base_url: str,
        login_url: str,
        username: str,
        password: str,
        request_delay: float,
        max_retries: int,
        timeout: int,
        debug_dir: Any,
        debug_enabled: bool,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.login_url = login_url
        self.username = username
        self.password = password
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.debug_dir = debug_dir
        self.debug_enabled = debug_enabled
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36 TrabBit/2.0"
            ),
            "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
        })
        self._last_request = 0.0

    def _respect_delay(self) -> None:
        remaining = self.request_delay - (time.monotonic() - self._last_request)
        if remaining > 0:
            time.sleep(remaining)

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        for attempt in range(self.max_retries + 1):
            self._respect_delay()
            response = self.session.get(
                url,
                timeout=self.timeout,
                allow_redirects=True,
                **kwargs,
            )
            self._last_request = time.monotonic()
            if response.status_code != 429:
                return response
            retry_after = response.headers.get("Retry-After")
            try:
                delay = float(retry_after) if retry_after else 5.0 * (2**attempt)
            except ValueError:
                delay = 5.0 * (2**attempt)
            delay = min(max(delay, 5.0), 60.0)
            log.warning("Toloka 429: чекаю %.1f с (%d/%d)", delay, attempt + 1, self.max_retries)
            time.sleep(delay)
        raise TolokaRateLimitError("Toloka продовжує повертати 429.")

    def login(self) -> None:
        if not self.username or not self.password:
            raise RuntimeError("TOLOKA_USERNAME/TOLOKA_PASSWORD не задані.")

        log.info("Виконую вхід у Толоку як %s", self.username)
        login_page = self._get(self.login_url, headers={"Referer": self.base_url + "/"})
        login_page.raise_for_status()

        data = {
            "username": self.username,
            "password": self.password,
            "autologin": "on",
            "ssl": "on",
            "redirect": "",
            "login": "Вхід",
        }
        self._respect_delay()
        response = self.session.post(
            self.login_url,
            data=data,
            headers={
                "Referer": login_page.url,
                "Origin": self.base_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=self.timeout,
            allow_redirects=False,
        )
        self._last_request = time.monotonic()

        if response.status_code in {302, 303}:
            location = response.headers.get("Location") or "/"
            verify_url = urljoin(self.base_url + "/", location)
            verify = self._get(verify_url, headers={"Referer": self.login_url})
            verify.raise_for_status()
            log.info("Вхід у Толоку виконано.")
            return

        if response.status_code == 429:
            raise TolokaRateLimitError("Toloka повернула 429 під час входу.")

        if response.status_code not in {200, 302, 303}:
            response.raise_for_status()

        if self.debug_enabled:
            ensure_dir(self.debug_dir)
            (self.debug_dir / "toloka_login_response.html").write_text(response.text, encoding="utf-8")
        raise RuntimeError("Не вдалося підтвердити вхід у Toloka.")

    def fetch_rss(self, rss_url: str) -> feedparser.FeedParserDict:
        response = self._get(rss_url, headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"})
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        log.info("RSS записів отримано: %d", len(feed.entries))
        return feed

    def fetch_topic_page(self, topic_url: str, topic_id: str | None = None) -> tuple[str | None, TopicDetails]:
        """Fetch and parse one topic page once. Returns torrent URL + structured metadata."""
        log.info("  Відкриваю тему: %s", topic_url)
        response = self._get(topic_url, headers={"Referer": self.base_url + "/"})
        response.raise_for_status()
        torrent_url, details = parse_topic_page(response.text, response.url)

        if self.debug_enabled:
            ensure_dir(self.debug_dir)
            filename = f"topic_{topic_id or 'unknown'}.html"
            (self.debug_dir / filename).write_text(response.text, encoding="utf-8")

        if torrent_url is None:
            log.warning("  Не знайшов torrent URL у темі %s", topic_id or "")
        else:
            log.info("  Знайдено torrent URL: %s", torrent_url)

        log.debug(
            "  Topic details: genres=%s country=%s studio=%s quality=%s source=%s translator=%s",
            details.genres, details.country, details.studio, details.quality, details.source, details.translator,
        )
        return torrent_url, details

    def resolve_torrent_url(self, topic_url: str, topic_id: str | None = None) -> str | None:
        torrent_url, _ = self.fetch_topic_page(topic_url, topic_id)
        return torrent_url

    def download_torrent(self, url: str) -> tuple[bytes, str, int, str, str]:
        if url.lower().startswith("magnet:"):
            raise ValueError("Magnet поки не підтримується для попереднього визначення розміру.")
        response = self._get(url, headers={"Referer": self.base_url + "/"})
        response.raise_for_status()
        name, size, fingerprint, info_hash = parse_torrent(response.content)
        return response.content, name, size, fingerprint, info_hash

    @staticmethod
    def parse_topic(entry: Any) -> tuple[str | None, str, str, str, str]:
        topic_url = str(entry.get("link") or "").strip()
        title = str(entry.get("title") or "(без назви)").strip()
        creator = str(entry.get("creator") or entry.get("dc_creator") or "").strip()
        subject = str(entry.get("subject") or entry.get("dc_subject") or "").strip()
        match = re.search(r"/t(\d+)", topic_url, re.IGNORECASE)
        topic_id = match.group(1) if match else None
        return topic_id, topic_url, title, creator, subject

    def fetch_meta(self, topic_id: str, topic_url: str, title: str, creator: str = "", subject: str = "", watched: bool = False, priority: str = "normal") -> TorrentMeta | None:
        torrent_url, details = self.fetch_topic_page(topic_url, topic_id)
        if not torrent_url:
            return None
        data, torrent_name, size, fingerprint, info_hash = self.download_torrent(torrent_url)
        return TorrentMeta(
            topic_id=topic_id,
            topic_url=topic_url,
            topic_title=title,
            torrent_url=torrent_url,
            torrent_name=torrent_name,
            data=data,
            size_bytes=size,
            fingerprint=fingerprint,
            info_hash=info_hash,
            creator=creator,
            subject=subject,
            watched=watched,
            priority=priority,
            topic_details=details,
        )
