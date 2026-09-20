from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import threading
import time
import webbrowser

import tkinter as tk
from tkinter import messagebox, simpledialog

import pystray
from PIL import Image, ImageDraw, ImageFont

from trabbit.config import load_settings
from trabbit.manager import Manager
from trabbit.models import RetentionCandidate
from trabbit.utils import human_size


BASE_DIR = Path(__file__).resolve().parent
LOG_PATH = BASE_DIR / "trabbit.log"


def setup_logging() -> None:
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)


class TrayState:
    def __init__(self, poll_interval_minutes: float) -> None:
        self.lock = threading.Lock()
        self.poll_interval_seconds = max(1.0, poll_interval_minutes * 60.0)
        self.running = False
        self.paused = False
        self.last_status = "Очікування першої перевірки"
        self.last_error = ""
        self.last_run = "—"
        self.next_run = "—"
        self.used_bytes = 0
        self.limit_bytes = 0
        self.last_seen = 0
        self.cleanup_candidates: list[RetentionCandidate] = []

    def update(self, **values: object) -> None:
        with self.lock:
            for key, value in values.items():
                setattr(self, key, value)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return self.__dict__.copy()


class TrabBitTray:
    def __init__(self) -> None:
        os.chdir(BASE_DIR)
        setup_logging()
        self.log = logging.getLogger("TrabBit.tray")

        self.settings = load_settings(BASE_DIR / ".env")
        self.settings.save_path.mkdir(parents=True, exist_ok=True)
        self.settings.qbit_base_path.mkdir(parents=True, exist_ok=True)
        if self.settings.debug_save_html:
            self.settings.debug_dir.mkdir(parents=True, exist_ok=True)

        interval = float(os.getenv("TRAY_POLL_INTERVAL_MINUTES", "30"))
        self.start_delay = max(0.0, float(os.getenv("TRAY_START_DELAY_SECONDS", "15")))
        self.state = TrayState(interval)
        self.state.limit_bytes = self.settings.max_bytes()

        self.stop_event = threading.Event()
        self.run_now_event = threading.Event()
        self.cycle_lock = threading.Lock()
        # Manager owns thread-bound SQLite and network session objects.
        # Create a Manager inside the thread that actually uses it.
        self.worker = threading.Thread(target=self._worker_loop, name="TrabBitWorker", daemon=True)

        self.icon = pystray.Icon(
            "TrabBit",
            self._make_icon(),
            "TrabBit",
            menu=self._build_menu(),
        )

    @staticmethod
    def _make_icon() -> Image.Image:
        image = Image.new("RGB", (64, 64), "#252525")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((4, 4, 60, 60), radius=12, fill="#e8e8e8")
        draw.rectangle((12, 28, 52, 48), fill="#252525")
        draw.rectangle((18, 20, 46, 28), fill="#252525")
        try:
            font = ImageFont.truetype("segoeui.ttf", 16)
        except OSError:
            font = ImageFont.load_default()
        draw.text((21, 31), "TB", fill="#e8e8e8", font=font)
        return image

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(lambda _: self.status_text(), None, enabled=False),
            pystray.MenuItem(lambda _: self.storage_text(), None, enabled=False),
            pystray.MenuItem(lambda _: self.retention_text(), self.review_retention),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Перевірити зараз", self.manual_run),
            pystray.MenuItem(
                lambda _: "Продовжити автоматичні перевірки" if self.state.snapshot()["paused"] else "Поставити на паузу",
                self.toggle_pause,
            ),
            pystray.MenuItem("Відкрити qBittorrent", self.open_qbit),
            pystray.MenuItem("Відкрити журнал", self.open_log),
            pystray.MenuItem("Відкрити теку", pystray.Menu(
                pystray.MenuItem("Теку TrabBit", self.open_project),
                pystray.MenuItem("Теку завантаження торентів", self.open_torrent_folder),
            )),
            pystray.MenuItem("Watchlist", pystray.Menu(
                pystray.MenuItem("Додати тему…", self.add_watch_dialog),
                pystray.MenuItem("Видалити тему…", self.remove_watch_dialog),
                pystray.MenuItem("Показати список", self.open_cli_watchlist),
            )),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("CLI", pystray.Menu(
                pystray.MenuItem("Retag existing", self.open_cli_retag),
                pystray.MenuItem("Відкрити PowerShell у проєкті", self.open_cli_shell),
            )),
            pystray.MenuItem("Вийти", self.exit),
        )

    def status_text(self) -> str:
        state = self.state.snapshot()
        if state["running"]:
            return "TrabBit: виконується перевірка…"
        if state["paused"]:
            return "TrabBit: пауза"
        return f"TrabBit: {state['last_status']}"

    def storage_text(self) -> str:
        state = self.state.snapshot()
        used = human_size(int(state["used_bytes"]))
        limit = human_size(int(state["limit_bytes"]))
        return f"Сховище: {used} / {limit}"

    def retention_text(self) -> str:
        state = self.state.snapshot()
        count = len(state.get("cleanup_candidates", []))
        return f"Кандидати на очищення: {count}" if count else "Кандидати на очищення: немає"

    def _notify(self, title: str, message: str) -> None:
        try:
            self.icon.notify(message, title)
        except Exception:
            self.log.debug("Не вдалося показати Windows notification", exc_info=True)

    def _worker_loop(self) -> None:
        next_cycle = time.monotonic() + self.start_delay

        while not self.stop_event.is_set():
            timeout = max(0.0, next_cycle - time.monotonic())
            if self.run_now_event.wait(timeout=timeout):
                self.run_now_event.clear()
                if self.stop_event.is_set():
                    return
                if not self.state.snapshot()["paused"]:
                    self._run_cycle()
                next_cycle = time.monotonic() + self.state.poll_interval_seconds
                continue

            if self.stop_event.is_set():
                return

            if not self.state.snapshot()["paused"]:
                self._run_cycle()
                next_cycle = time.monotonic() + self.state.poll_interval_seconds
            else:
                next_cycle = time.monotonic() + 5.0

    def _run_cycle(self) -> None:
        if not self.cycle_lock.acquire(blocking=False):
            self.log.info("Перевірка вже виконується, пропускаю повторний запуск.")
            return

        manager = None
        try:
            self.state.update(running=True, last_status="підключення")
            self.log.info("=== Початок автоматичної перевірки ===")

            manager = Manager(self.settings)
            manager.login()
            result = manager.run_cycle(include_watchlist=True)
            candidates = result.retention_candidates

            self.state.update(
                running=False,
                last_status="готово",
                last_error="",
                last_run=time.strftime("%Y-%m-%d %H:%M:%S"),
                used_bytes=result.used_bytes,
                last_seen=len(result.processed_topics),
                cleanup_candidates=candidates,
            )
            self.icon.update_menu()
            if candidates:
                self._notify(
                    "TrabBit",
                    f"Знайдено {len(candidates)} кандидатів на очищення сховища. Відкрий меню трея для підтвердження.",
                )
        except Exception as exc:
            self.log.exception("Фонова перевірка завершилась помилкою")
            self.state.update(
                running=False,
                last_status="помилка",
                last_error=str(exc),
                last_run=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            self._notify("TrabBit", f"Фонова перевірка завершилась помилкою: {exc}")
        finally:
            if manager is not None:
                try:
                    manager.close()
                except Exception:
                    self.log.exception("Помилка закриття Manager після перевірки")
            self.state.update(next_run=time.strftime("%Y-%m-%d %H:%M:%S"))
            self.cycle_lock.release()

    def manual_run(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        self.run_now_event.set()
        self.log.info("Запит на ручну перевірку отримано з трею.")

    def review_retention(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        candidates = list(self.state.snapshot().get("cleanup_candidates", []))
        if not candidates:
            self._notify("TrabBit", "Кандидатів на видалення зараз немає.")
            return

        confirmed: list[RetentionCandidate] = []
        for candidate in candidates:
            files_text = (
                "ФАЙЛИ будуть видалені"
                if candidate.delete_files
                else "Файли НЕ будуть видалені (захист від спільних даних із новішою ревізією)"
            )
            message = (
                "Видалити torrent?\n\n"
                f"{candidate.title}\n"
                f"Розмір: {human_size(candidate.size_bytes)}\n"
                f"Score: {candidate.score:.1f}\n"
                f"Ratio: {candidate.ratio:.2f}\n"
                f"Popularity: {candidate.popularity:.2f}\n"
                f"Seeds/Leechs: {candidate.num_seeds}/{candidate.num_leechs}\n"
                f"Причина: {candidate.reason}\n"
                f"{files_text}"
            )
            answer = self._dialog(
                lambda root, msg=message: messagebox.askyesno(
                    "TrabBit · Підтвердження видалення",
                    msg,
                    parent=root,
                )
            )
            if answer:
                confirmed.append(candidate)

        if not confirmed:
            return

        def job() -> None:
            if not self.cycle_lock.acquire(blocking=False):
                self._notify("TrabBit", "Зараз уже виконується інша операція.")
                return

            manager = None
            try:
                self.state.update(running=True, last_status="очищення сховища")
                manager = Manager(self.settings)
                manager.login()
                current = manager.qbit.get_torrents_by_hash()
                for candidate in confirmed:
                    try:
                        if candidate.info_hash not in current:
                            self.log.info("Retention: %s вже відсутній у qBittorrent.", candidate.title)
                            continue
                        manager.delete_retention_candidate(candidate)
                    except Exception:
                        self.log.exception("Retention: не вдалося видалити %s", candidate.title)

                remaining = manager.scan_retention_candidates()
                used = manager.current_managed_size()
                self.state.update(
                    running=False,
                    last_status="очищення завершено",
                    last_error="",
                    used_bytes=used,
                    cleanup_candidates=remaining,
                )
                self.icon.update_menu()
            except Exception as exc:
                self.log.exception("Retention: критична помилка очищення")
                self.state.update(running=False, last_status="помилка", last_error=str(exc))
                self._notify("TrabBit", f"Помилка очищення: {exc}")
            finally:
                if manager is not None:
                    try:
                        manager.close()
                    except Exception:
                        self.log.exception("Помилка закриття Manager після retention")
                self.cycle_lock.release()

        threading.Thread(target=job, name="TrabBitRetention", daemon=True).start()

    def toggle_pause(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        current = bool(self.state.snapshot()["paused"])
        self.state.update(paused=not current, last_status="пауза" if not current else "відновлено")
        self.log.info("Автоматичні перевірки: %s", "пауза" if not current else "увімкнено")
        if current:
            self.manual_run()

    def open_qbit(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        webbrowser.open(self.settings.qbit_host)

    def open_log(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        try:
            os.startfile(str(LOG_PATH))  # type: ignore[attr-defined]
        except FileNotFoundError:
            LOG_PATH.touch()
            os.startfile(str(LOG_PATH))  # type: ignore[attr-defined]

    def open_project(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        os.startfile(str(BASE_DIR))  # type: ignore[attr-defined]

    def open_torrent_folder(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        os.startfile(str(self.settings.save_path))  # type: ignore[attr-defined]

    def _start_cli(self, command: str) -> None:
        python_exe = BASE_DIR / ".venv" / "Scripts" / "python.exe"
        if not python_exe.exists():
            python_exe = Path(os.sys.executable)
        subprocess.Popen(
            [str(python_exe), *command.split()],
            cwd=str(BASE_DIR),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )

    @staticmethod
    def _dialog(func):
        root = tk.Tk()
        root.withdraw()
        try:
            return func(root)
        finally:
            root.destroy()

    def add_watch_dialog(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        topic = self._dialog(lambda root: simpledialog.askstring(
            "TrabBit · Watchlist",
            "Введи tXXXXX або URL теми Toloka:",
            parent=root,
        ))
        if not topic:
            return

        priority = self._dialog(lambda root: simpledialog.askstring(
            "TrabBit · Watchlist",
            "Пріоритет: critical / high / normal / low",
            initialvalue="normal",
            parent=root,
        )) or "normal"
        priority = priority.strip().lower()
        if priority not in {"critical", "high", "normal", "low"}:
            self._dialog(lambda root: messagebox.showerror(
                "TrabBit", "Некоректний пріоритет.", parent=root
            ))
            return

        def job() -> None:
            if not self.cycle_lock.acquire(blocking=False):
                self._notify("TrabBit", "Зараз уже виконується інша операція.")
                return

            manager = None
            try:
                self.state.update(running=True, last_status="додаю тему")
                manager = Manager(self.settings)
                manager.add_watch(topic, priority=priority, auto_add=True, auto_update=True)
                self.state.update(running=False, last_status=f"watchlist: додано {topic}")
                self._notify("TrabBit", f"Тему {topic} додано до watchlist.")
            except Exception as exc:
                self.log.exception("Помилка додавання watchlist")
                self.state.update(running=False, last_status="помилка", last_error=str(exc))
                self._notify("TrabBit", f"Не вдалося додати тему: {exc}")
            finally:
                if manager is not None:
                    try:
                        manager.close()
                    except Exception:
                        self.log.exception("Помилка закриття Manager після додавання watchlist")
                self.cycle_lock.release()

        threading.Thread(target=job, name="TrabBitWatchAdd", daemon=True).start()

    def remove_watch_dialog(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        topic = self._dialog(lambda root: simpledialog.askstring(
            "TrabBit · Watchlist",
            "Введи tXXXXX або URL теми, яку прибрати:",
            parent=root,
        ))
        if not topic:
            return

        def job() -> None:
            if not self.cycle_lock.acquire(blocking=False):
                self._notify("TrabBit", "Зараз уже виконується інша операція.")
                return

            manager = None
            try:
                self.state.update(running=True, last_status="видаляю тему")
                manager = Manager(self.settings)
                manager.remove_watch(topic)
                self.state.update(running=False, last_status=f"watchlist: видалено {topic}")
                self._notify("TrabBit", f"Тему {topic} прибрано з watchlist.")
            except Exception as exc:
                self.log.exception("Помилка видалення watchlist")
                self.state.update(running=False, last_status="помилка", last_error=str(exc))
                self._notify("TrabBit", f"Не вдалося видалити тему: {exc}")
            finally:
                if manager is not None:
                    try:
                        manager.close()
                    except Exception:
                        self.log.exception("Помилка закриття Manager після видалення watchlist")
                self.cycle_lock.release()

        threading.Thread(target=job, name="TrabBitWatchRemove", daemon=True).start()

    def open_cli_watchlist(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        self._start_cli("main.py --watch-list")

    def open_cli_retag(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        self._start_cli("main.py --retag-existing")

    def open_cli_shell(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        subprocess.Popen(["cmd.exe", "/K", f'cd /d "{BASE_DIR}"'], cwd=str(BASE_DIR))

    def exit(self, icon: pystray.Icon | None = None, item: pystray.MenuItem | None = None) -> None:
        self.stop_event.set()
        self.run_now_event.set()
        self.icon.stop()

    def run(self) -> int:
        self.log.info("TrabBit tray запускається, інтервал=%s хв", self.state.poll_interval_seconds / 60)
        self.worker.start()
        self.icon.run()
        self.stop_event.set()
        self.run_now_event.set()
        if self.worker.is_alive():
            self.worker.join(timeout=10.0)
        return 0


def run_tray() -> int:
    app = TrabBitTray()
    return app.run()


if __name__ == "__main__":
    raise SystemExit(run_tray())
