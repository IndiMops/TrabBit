from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TopicDetails:
    age_restricted: bool = False
    genres: list[str] = field(default_factory=list)
    country: str = ""
    studio: str = ""
    director: str = ""
    voice_actors: list[str] = field(default_factory=list)
    duration: str = ""
    episode_current: int | None = None
    episode_total: int | None = None
    quality: str = ""
    video_codec: str = ""
    video_width: int | None = None
    video_height: int | None = None
    video_bitrate: str = ""
    audio_languages: list[str] = field(default_factory=list)
    audio_translations: list[str] = field(default_factory=list)
    audio_codecs: list[str] = field(default_factory=list)
    subtitle_languages: list[str] = field(default_factory=list)
    subtitle_types: list[str] = field(default_factory=list)
    subtitle_formats: list[str] = field(default_factory=list)
    source: str = ""
    translator: str = ""
    voice_roles: list[str] = field(default_factory=list)
    sound_work: str = ""
    raw_text: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "age_restricted": self.age_restricted,
            "genres": self.genres,
            "country": self.country,
            "studio": self.studio,
            "director": self.director,
            "voice_actors": self.voice_actors,
            "duration": self.duration,
            "episode_current": self.episode_current,
            "episode_total": self.episode_total,
            "quality": self.quality,
            "video_codec": self.video_codec,
            "video_width": self.video_width,
            "video_height": self.video_height,
            "video_bitrate": self.video_bitrate,
            "audio_languages": self.audio_languages,
            "audio_translations": self.audio_translations,
            "audio_codecs": self.audio_codecs,
            "subtitle_languages": self.subtitle_languages,
            "subtitle_types": self.subtitle_types,
            "subtitle_formats": self.subtitle_formats,
            "source": self.source,
            "translator": self.translator,
            "voice_roles": self.voice_roles,
            "sound_work": self.sound_work,
            "raw_text": self.raw_text,
        }


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
    topic_details: TopicDetails = field(default_factory=TopicDetails)


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
