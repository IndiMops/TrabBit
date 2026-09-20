from pathlib import Path

from trabbit.rules import RulesEngine
from trabbit.topic_parser import parse_topic_page


HTML = """
<div>
<b>Пані Дара з епохи Рейва (Сезон 1, серії 1-12 з 13) WEBRip Ai Rem 1080p H.265 Ukr/Jap | Sub Ukr</b><br>
<b>Увага! Реліз 18+</b><br>
<b>Жанр:</b> комедія, надприродне, мітологія<br>
<b>Країна:</b> Японія<br>
<b>Кінокомпанія:</b> Asahi Production<br>
<b>Якість:</b> WEBRip Ai Rem<br>
<b>Відео:</b><br>кодек: H.265<br>розмір кадру: 1920 х 1080<br>бітрейт: 1590 кб/с<br>
<b>Аудіо #1:</b><br>мова: українська<br>переклад: багатоголосий закадровий<br>кодек: AAC 2.0<br>
<b>Аудіо #2:</b><br>мова: японська<br>переклад: оригінал<br>кодек: AAC 2.0<br>
<b>Субтитри:</b><br>мова: українська<br>тип: програмні (м'які)<br>формат: *.ass<br>
<b>Джерело:</b> Anitube.in.ua<br>
<b>Переклад:</b> Aliceinhunterlnd<br>
<a href="/download.php?id=714999">Завантажити</a>
</div>
"""


def main() -> None:
    torrent_url, details = parse_topic_page(HTML, "https://toloka.to/t699103")
    rules = RulesEngine(Path("config/rules.json"))
    category, tags = rules.classify(
        title="Пані Дара Сезон 1 WEBRip 1080p H.265",
        torrent_name="Reiwa no Dara-san [WEBRip 1080p HEVC]",
        topic_details=details,
    )
    assert torrent_url.endswith("download.php?id=714999")
    assert category == "Anime"
    assert details.age_restricted
    assert details.video_codec == "H.265"
    assert details.video_width == 1920
    assert details.video_height == 1080
    assert details.translator == "Aliceinhunterlnd"
    assert "18plus" in tags
    assert "genre-комедія" in tags
    assert "audio-ukr" in tags
    assert "audio-japanese" in tags
    assert "dub-multivoice" in tags
    assert "sub-ass" in tags
    assert "source-anitube-in-ua" in tags
    assert "translator-aliceinhunterlnd" in tags
    print("Topic parser test: OK")


if __name__ == "__main__":
    main()
