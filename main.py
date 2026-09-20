from __future__ import annotations

import argparse
import logging
import sys

from trabbit.config import load_settings
from trabbit.manager import Manager


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TrabBit Toloka Seed Manager v2.0")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--watch-add", metavar="TOPIC", help="Додати tXXXXX або URL у watchlist")
    group.add_argument("--watch-remove", metavar="TOPIC", help="Видалити тему з watchlist")
    group.add_argument("--watch-list", action="store_true", help="Показати watchlist")
    group.add_argument("--retag-existing", action="store_true", help="Привести існуючі торренти до нових category/tag правил")
    parser.add_argument("--priority", choices=["critical", "high", "normal", "low"], default="normal", help="Пріоритет для --watch-add")
    parser.add_argument("--watch-auto-add", action="store_true", help="Увімкнути auto-add для --watch-add")
    parser.add_argument("--watch-auto-update", action="store_true", help="Увімкнути auto-update для --watch-add")
    parser.add_argument("--watch-interval", type=int, default=1440, help="Інтервал watchlist у хвилинах")
    parser.add_argument("--no-watchlist", action="store_true", help="Не перевіряти watchlist у цьому запуску")
    return parser


def print_config(settings) -> None:
    print()
    print("=" * 72)
    print(" TrabBit Toloka Seed Manager v2.0")
    print("=" * 72)
    print()
    print(f"RSS:                {settings.rss_url}")
    print(f"QBit path:          {settings.qbit_base_path}")
    print(f"Limit:              {settings.torrent_limit_gb:.2f} GB")
    print(f"Warning:            {settings.torrent_warning_gb:.2f} GB")
    print(f"AUTO_ADD_NEW:       {'YES' if settings.auto_add_new else 'NO'}")
    print(f"AUTO_UPDATE:        {'YES' if settings.auto_update else 'NO'}")
    print(f"WATCHLIST_AUTO_ADD: {'YES' if settings.watchlist_auto_add else 'NO'}")
    print(f"WATCHLIST_AUTO_UPDATE:{'YES' if settings.watchlist_auto_update else 'NO'}")
    print(f"READD_MISSING:      {'YES' if settings.readd_missing else 'NO'}")
    print(f"DRY_RUN:            {'YES' if settings.dry_run else 'NO'}")
    print(f"Managed tag:        {settings.managed_tag}")
    print(f"Ignore tag:         {settings.ignore_tag}")
    print(f"Legacy categories:  {", ".join(settings.legacy_managed_categories) or "(none)"}")
    print(f"Toloka delay:       {settings.toloka_request_delay:.1f} s")
    print()


def main() -> int:
    args = build_parser().parse_args()
    settings = load_settings()
    print_config(settings)

    settings.save_path.mkdir(parents=True, exist_ok=True)
    settings.qbit_base_path.mkdir(parents=True, exist_ok=True)
    if settings.debug_save_html:
        settings.debug_dir.mkdir(parents=True, exist_ok=True)

    manager = Manager(settings)
    try:
        # Watchlist-only admin commands should not require qBittorrent/Toloka at every call.
        if args.watch_list:
            manager.print_watchlist()
            return 0

        if args.watch_add:
            manager.add_watch(
                args.watch_add,
                priority=args.priority,
                auto_add=True if args.watch_auto_add else None,
                auto_update=True if args.watch_auto_update else None,
                interval_minutes=args.watch_interval,
            )
            manager.print_watchlist()
            return 0

        if args.watch_remove:
            manager.remove_watch(args.watch_remove)
            manager.print_watchlist()
            return 0

        manager.login()

        if args.retag_existing:
            manager.retag_existing()
            return 0

        processed = manager.process_rss()
        if not args.no_watchlist:
            manager.process_watchlist(processed)
        return 0

    except Exception as exc:
        logging.getLogger("TrabBit").exception("Критична помилка: %s", exc)
        return 1
    finally:
        manager.close()


if __name__ == "__main__":
    sys.exit(main())
