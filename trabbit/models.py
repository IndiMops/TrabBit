from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TorrentMeta:
    topic_id: str
    topic_url: str
    topic_title: str
    torrent_url: str
    torrent_name: str
    data: bytes
    size_bytes: int
    fingerprint: str
    info_hash: str
    creator: str = ""
    subject: str = ""
    watched: bool = False
    priority: str = "normal"
    category: str = "Other"
    tags: list[str] = field(default_factory=list)


@dataclass
class Decision:
    action: str
    reason: str
    category: str
    tags: list[str]


@dataclass
class ManagedTorrent:
    info_hash: str
    topic_id: str | None
    title: str
    size_bytes: int
    category: str
    tags: list[str]
    source: str
    added_at: str | None = None
    last_seen_at: str | None = None
    qbit_present: bool = False
    status: str = "seen"


def torrent_from_row(row: Any) -> ManagedTorrent:
    import json

    return ManagedTorrent(
        info_hash=row["info_hash"],
        topic_id=row["topic_id"],
        title=row["title"],
        size_bytes=int(row["size_bytes"] or 0),
        category=row["category"],
        tags=json.loads(row["tags_json"] or "[]"),
        source=row["source"],
        added_at=row["added_at"],
        last_seen_at=row["last_seen_at"],
        qbit_present=bool(row["qbit_present"]),
        status=row["status"],
    )
