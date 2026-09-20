from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import feedparser

from trabbit.config import load_settings
from trabbit.toloka import TolokaClient


DEFAULT_TEST_URL = "https://toloka.to/rss.php?t=1&c=50&f=127-90&toronly=1&lite=1&thumbs=2"


def topic_id_from_url(url: str) -> str:
    match = re.search(r"/t(\d+)", url or "", re.IGNORECASE)
    return match.group(1) if match else "?"


def build_client(settings):
    return TolokaClient(
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TrabBit helper для тестування RSS Toloka через авторизовану сесію."
    )
    parser.add_argument(
        "--url",
        default=None,
        help="RSS URL для перевірки. За замовчуванням RSS_TEST_URL або тестовий c=20.",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=10,
        help="Скільки записів показати (за замовчуванням 10). 0 = нічого.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Показати всі записи RSS.",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Зберегти отриманий RSS XML у debug/rss_tests/.",
    )
    args = parser.parse_args()

    settings = load_settings()
    rss_url = args.url or __import__("os").getenv("RSS_TEST_URL") or DEFAULT_TEST_URL

    print("=" * 78)
    print(" TrabBit RSS Test")
    print("=" * 78)
    print(f"RSS URL:       {rss_url}")
    print(f"Toloka user:   {settings.toloka_username}")
    print(f"Request delay: {settings.toloka_request_delay:.1f} s")
    print()

    client = build_client(settings)

    try:
        print("[1/3] Авторизація в Toloka...")
        client.login()
        print("      OK")
        print()

        print("[2/3] Отримання RSS через авторизовану сесію...")
        response = client._get(
            rss_url,
            headers={
                "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"
            },
        )
        response.raise_for_status()
        print(f"      HTTP:        {response.status_code}")
        print(f"      Content-Type: {response.headers.get('content-type', '(none)')}")
        print(f"      Bytes:        {len(response.content)}")
        print()

        if not response.content.strip():
            print("RSS повернув порожню відповідь.")
            return 2

        feed = feedparser.parse(response.content)
        print("[3/3] Розбір RSS...")
        print(f"      Entries:      {len(feed.entries)}")
        print(f"      Bozo:         {getattr(feed, 'bozo', False)}")
        if getattr(feed, "bozo_exception", None):
            print(f"      Bozo error:   {feed.bozo_exception}")
        print()

        if args.save:
            save_dir = Path(settings.debug_dir) / "rss_tests"
            save_dir.mkdir(parents=True, exist_ok=True)
            stamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = save_dir / f"rss_{stamp}.xml"
            save_path.write_bytes(response.content)
            print(f"Збережено RSS: {save_path.resolve()}")
            print()

        entries = feed.entries if args.all else feed.entries[: max(args.show, 0)]
        if entries:
            print("Записи:")
            for index, entry in enumerate(entries, start=1):
                title = str(entry.get("title") or "(без назви)").strip()
                link = str(entry.get("link") or "").strip()
                creator = str(entry.get("creator") or entry.get("dc_creator") or "").strip()
                topic_id = topic_id_from_url(link)
                print(f"{index:>3}. t{topic_id} | {creator or '-'} | {title}")
                print(f"     {link}")

        if feed.entries:
            first_link = str(feed.entries[0].get("link") or "")
            last_link = str(feed.entries[-1].get("link") or "")
            print()
            print("Діапазон RSS:")
            print(f"  first: t{topic_id_from_url(first_link)}")
            print(f"  last:  t{topic_id_from_url(last_link)}")

        return 0

    except Exception as exc:
        print()
        print(f"ПОМИЛКА: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.session.close()


if __name__ == "__main__":
    raise SystemExit(main())
