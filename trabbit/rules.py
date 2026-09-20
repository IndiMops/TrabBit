from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import TopicDetails
from .topic_parser import build_detail_tags


class RulesEngine:
    def __init__(self, path: Path) -> None:
        self.path = path
        with path.open("r", encoding="utf-8") as fh:
            self.data: dict[str, Any] = json.load(fh)

        self.default_category = self.data.get("default_category", "Other")
        self.category_paths = self.data.get("category_paths", {})
        self.category_rules = self.data.get("category_rules", [])
        self.tag_rules = self.data.get("tag_rules", [])

    def classify(
        self,
        *,
        title: str,
        torrent_name: str,
        creator: str = "",
        subject: str = "",
        watched: bool = False,
        priority: str = "normal",
        topic_details: TopicDetails | None = None,
    ) -> tuple[str, list[str]]:
        detail_values: list[str] = []
        if topic_details:
            detail_values.extend(topic_details.genres)
            detail_values.extend([topic_details.country, topic_details.studio, topic_details.director])
            detail_values.extend(topic_details.audio_languages)
            detail_values.extend(topic_details.audio_translations)
            detail_values.extend(topic_details.audio_codecs)
            detail_values.extend(topic_details.subtitle_languages)
            detail_values.extend(topic_details.subtitle_formats)
            detail_values.extend([topic_details.quality, topic_details.video_codec, topic_details.source, topic_details.translator])
        haystack = "\n".join([title, torrent_name, creator, subject, *detail_values]).lower()

        category = self.default_category
        for rule in self.category_rules:
            patterns = rule.get("patterns", [])
            if any(re.search(pattern, haystack, re.IGNORECASE) for pattern in patterns):
                category = rule["category"]
                break

        tags: list[str] = []
        for rule in self.tag_rules:
            tag = rule.get("tag")
            patterns = rule.get("patterns", [])
            if tag and any(re.search(pattern, haystack, re.IGNORECASE) for pattern in patterns):
                tags.append(tag)

        # Source tag is deterministic because this manager is specifically Toloka-based.
        tags.append("toloka")

        # Category becomes a useful filter, too.
        category_tag = category.lower().replace(" ", "-")
        if category_tag:
            tags.append(category_tag)

        # Episode progress: "1-4 z 13", "серії 1-11 з 12", etc.
        progress = re.search(r"(?:сер(?:ія|ії)|episode(?:s)?)\s*([0-9]+)(?:\s*[-–]\s*([0-9]+))?\s+(?:з|of)\s+([0-9]+)", haystack, re.IGNORECASE)
        if progress:
            current = int(progress.group(2) or progress.group(1))
            total = int(progress.group(3))
            tags.append("complete" if current >= total else "ongoing")

        # Simple completion markers.
        if re.search(r"\b(?:complete|completed|повне|повністю)\b", haystack, re.IGNORECASE):
            tags.append("complete")

        # Season tags.
        season = re.search(r"(?:сезон|season)\s*0*(\d+)", haystack, re.IGNORECASE)
        if season:
            tags.append(f"s{int(season.group(1)):02d}")

        if watched:
            tags.append("watchlist")

        if priority in {"critical", "high"}:
            tags.append(f"priority-{priority}")

        if topic_details:
            tags.extend(build_detail_tags(topic_details))

        return category, dedupe(tags)

    def managed_tag_names(self, managed_tag: str) -> set[str]:
        """Return tags that TrabBit may safely rebuild during deep retagging."""
        names = {managed_tag, "toloka", "watchlist", "complete", "ongoing", "18plus"}
        names.update(self.tag_rules[i].get("tag", "") for i in range(len(self.tag_rules)))
        names.update(name.lower().replace(" ", "-") for name in self.categories())
        prefixes = {
            "genre-", "country-", "studio-", "source-", "translator-",
            "audio-", "sub-", "dub-", "priority-"
        }
        for torrent_tag in self.data.get("known_tags", []):
            names.add(str(torrent_tag))
        # Dynamic tags cannot be enumerated, but their namespaces are owned by TrabBit.
        self._managed_prefixes = prefixes
        return {name for name in names if name}

    def is_managed_dynamic_tag(self, tag: str) -> bool:
        prefixes = getattr(self, "_managed_prefixes", {
            "genre-", "country-", "studio-", "source-", "translator-",
            "audio-", "sub-", "dub-", "priority-"
        })
        return any(tag.startswith(prefix) for prefix in prefixes) or bool(
            re.fullmatch(r"s\d{2}|(?:1080p|720p|2160p)|(?:hevc|h264|bdrip|bdremux|web-dl|webrip|dvdrip|ai-rem)", tag)
        )

    def category_path(self, category: str, base_path: Path) -> Path:
        relative = self.category_paths.get(category, category)
        return base_path / relative

    def categories(self) -> list[str]:
        names = [self.default_category]
        names.extend(
            rule["category"]
            for rule in self.category_rules
            if rule.get("category")
        )
        return dedupe(names)


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result
