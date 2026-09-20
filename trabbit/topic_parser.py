from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import TopicDetails


def _clean(value: str) -> str:
    value = re.sub(r"\s+", " ", value or "")
    return value.strip(" \t\r\n:;")


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in re.split(r",|;", value or "") if item.strip()]


def _slug(value: str) -> str:
    value = value.lower().strip()
    value = value.replace("ё", "е")
    value = re.sub(r"[^\w\-]+", "-", value, flags=re.UNICODE)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:60]


def _find_label_value(text: str, label: str) -> str:
    pattern = rf"(?:^|\n)\s*{re.escape(label)}\s*:\s*([^\n]+)"
    match = re.search(pattern, text, re.IGNORECASE)
    return _clean(match.group(1)) if match else ""


def _find_all_label_values(text: str, label: str) -> list[str]:
    pattern = rf"(?:^|\n)\s*{re.escape(label)}\s*:\s*([^\n]+)"
    return [_clean(m.group(1)) for m in re.finditer(pattern, text, re.IGNORECASE)]


def _find_last_label_value(text: str, label: str) -> str:
    values = _find_all_label_values(text, label)
    return values[-1] if values else ""


def _section(text: str, start_label: str, end_labels: list[str]) -> str:
    start = re.search(rf"(?:^|\n)\s*{re.escape(start_label)}\s*:\s*", text, re.IGNORECASE)
    if not start:
        return ""
    remainder = text[start.end():]
    positions = []
    for label in end_labels:
        match = re.search(rf"(?:^|\n)\s*{re.escape(label)}", remainder, re.IGNORECASE)
        if match:
            positions.append(match.start())
    end = min(positions) if positions else len(remainder)
    return remainder[:end]


def _parse_episode_progress(text: str) -> tuple[int | None, int | None]:
    patterns = [
        r"сер(?:ія|ії)\s*(?:№\s*)?(\d+)(?:\s*[-–]\s*(\d+))?\s+з\s+(\d+)",
        r"(?:episodes?)\s*(\d+)(?:\s*[-–]\s*(\d+))?\s+(?:of)\s+(\d+)",
        r"цілком\s*серій:\s*(\d+)\s+з\s+(\d+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            continue
        groups = [g for g in m.groups() if g is not None]
        if len(groups) == 3:
            return int(groups[1] or groups[0]), int(groups[2])
        if len(groups) == 2:
            return int(groups[0]), int(groups[1])
    return None, None


def parse_topic_page(html: str, page_url: str) -> tuple[str | None, TopicDetails]:
    soup = BeautifulSoup(html, "html.parser")

    # Remove script/style noise but preserve the page's visible text.
    for node in soup(["script", "style", "noscript"]):
        node.decompose()

    raw_text = soup.get_text("\n", strip=True)
    raw_text = re.sub(r"\n{2,}", "\n", raw_text)

    details = TopicDetails(raw_text=raw_text)
    lower = raw_text.lower()

    details.age_restricted = bool(
        re.search(r"18\+|реліз\s+18\s*\+|віков(?:е|ий)\s+обмеження", lower)
    )

    genres = _find_label_value(raw_text, "Жанр")
    details.genres = _split_list(genres)
    details.country = _find_label_value(raw_text, "Країна")
    details.studio = _find_label_value(raw_text, "Кінокомпанія")
    details.director = _find_label_value(raw_text, "Режисер")
    details.voice_actors = _split_list(_find_label_value(raw_text, "Ролі озвучують"))
    details.duration = _find_label_value(raw_text, "Тривалість")
    details.quality = _find_label_value(raw_text, "Якість")

    video_section = raw_text
    audio_marker = re.search(r"\bАудіо\s*#?\s*\d+\b", video_section, re.IGNORECASE)
    if audio_marker:
        video_section = video_section[:audio_marker.start()]
    details.video_codec = _find_label_value(video_section, "кодек")
    details.video_bitrate = _find_label_value(video_section, "бітрейт")

    frame = re.search(r"розмір кадру\s*:\s*(\d{3,5})\s*[xх×]\s*(\d{3,5})", video_section, re.IGNORECASE)
    if frame:
        details.video_width = int(frame.group(1))
        details.video_height = int(frame.group(2))

    current, total = _parse_episode_progress(raw_text)
    details.episode_current = current
    details.episode_total = total

    # Parse repeated Audio blocks approximately. The exact phpBB HTML differs between templates,
    # so the visible text parser deliberately uses labels rather than fragile CSS selectors.
    audio_blocks = re.split(r"\bАудіо\s*#?\s*\d+\b", raw_text, flags=re.IGNORECASE)[1:]
    for block in audio_blocks:
        lang = _find_label_value(block, "мова")
        translation = _find_label_value(block, "переклад")
        codec = _find_label_value(block, "кодек")
        if lang:
            details.audio_languages.append(lang)
        if translation:
            details.audio_translations.append(translation)
        if codec:
            details.audio_codecs.append(codec)

    subtitle_blocks = re.split(r"\bСубтитри\b", raw_text, flags=re.IGNORECASE)[1:]
    for block in subtitle_blocks:
        lang = _find_label_value(block, "мова")
        sub_type = _find_label_value(block, "тип")
        fmt = _find_label_value(block, "формат")
        if lang:
            details.subtitle_languages.append(lang)
        if sub_type:
            details.subtitle_types.append(sub_type)
        if fmt:
            details.subtitle_formats.append(fmt)

    details.source = _find_label_value(raw_text, "Джерело")
    details.translator = _find_last_label_value(raw_text, "Переклад")
    details.voice_roles = _split_list(_find_last_label_value(raw_text, "Ролі озвучили"))
    details.sound_work = _find_last_label_value(raw_text, "Робота зі звуком")

    # Try to locate a torrent download link directly on the same page.
    torrent_url: str | None = None
    for link in soup.find_all("a", href=True):
        href = str(link["href"]).strip()
        absolute = urljoin(page_url, href)
        low = absolute.lower()
        if ".torrent" in low or "download.php" in low:
            torrent_url = absolute
            break

    if torrent_url is None:
        raw_match = re.search(
            r"(?:https?://[^\"'<>\s]+)?download\.php\?[^\"'<>\s]+",
            html,
            re.IGNORECASE,
        )
        if raw_match:
            torrent_url = urljoin(page_url, raw_match.group(0))

    return torrent_url, details


def build_detail_tags(details: TopicDetails) -> list[str]:
    tags: list[str] = []

    if details.age_restricted:
        tags.append("18plus")

    for genre in details.genres[:8]:
        slug = _slug(genre)
        if slug:
            tags.append(f"genre-{slug}")

    if details.country:
        slug = _slug(details.country)
        if slug:
            tags.append(f"country-{slug}")

    if details.studio:
        slug = _slug(details.studio)
        if slug:
            tags.append(f"studio-{slug}")

    if details.quality:
        q = details.quality.lower()
        if "ai rem" in q:
            tags.append("ai-rem")
        if "webrip" in q:
            tags.append("webrip")
        elif "web-dl" in q or "webdl" in q:
            tags.append("web-dl")
        elif "bdrip" in q:
            tags.append("bdrip")
        elif "bdremux" in q:
            tags.append("bdremux")
        elif "dvdrip" in q:
            tags.append("dvdrip")

    for lang in details.audio_languages:
        ll = lang.lower()
        if "укра" in ll or "ukrain" in ll:
            tags.append("audio-ukr")
        elif "япон" in ll or "japan" in ll:
            tags.append("audio-japanese")
        elif "англ" in ll or "english" in ll:
            tags.append("audio-eng")

    for translation in details.audio_translations:
        tl = translation.lower()
        if "багатоголос" in tl:
            tags.append("dub-multivoice")
        elif "двоголос" in tl:
            tags.append("dub-dualvoice")
        elif "закадр" in tl:
            tags.append("dub-vo")
        elif "оригінал" not in tl:
            tags.append("dub")

    for lang in details.subtitle_languages:
        ll = lang.lower()
        if "укра" in ll or "ukrain" in ll:
            tags.append("sub-ukr")
        elif "англ" in ll or "english" in ll:
            tags.append("sub-eng")

    for fmt in details.subtitle_formats:
        fl = fmt.lower().lstrip("*.")
        if "ass" in fl:
            tags.append("sub-ass")
        elif "srt" in fl:
            tags.append("sub-srt")

    for marker in (details.source, details.translator):
        slug = _slug(marker)
        if slug:
            prefix = "source" if marker == details.source else "translator"
            tags.append(f"{prefix}-{slug}")

    return list(dict.fromkeys(tags))
