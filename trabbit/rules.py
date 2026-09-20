from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


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
    ) -> tuple[str, list[str]]:
        haystack = "\n".join([title, torrent_name, creator, subject]).lower()

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

        return category, dedupe(tags)

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
