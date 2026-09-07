"""Command-line entry point for ``python -m schedule_bot``."""

from __future__ import annotations

import argparse
import logging
import sys

from .calendar import WEEKDAY_NAMES_RU
from .settings import ConfigurationError, Settings
from .storage import BotStorage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Локальный Telegram-бот расписания")
    parser.add_argument(
        "--check",
        action="store_true",
        help="проверить config.json и наличие изображений без запуска Telegram",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="проверить постоянное хранилище для Docker healthcheck",
    )
    return parser


def _print_check(settings: Settings) -> None:
    from datetime import datetime, timedelta, timezone

    today = datetime.now(
        timezone(timedelta(hours=settings.utc_offset_hours))
    ).date()
    storage = BotStorage(
        settings.runtime_data_directory,
        settings.schedules_directory,
        settings.maps_directory,
        settings.academic_year_start_month,
        settings.academic_year_start_day,
    )
    storage.initialize(today)
    catalog = storage.active_catalog()
    kind = storage.week_kind(today)
    active = storage.active_semester()
    print(f"config.json: OK ({settings.bot_title})")
    print(f"Хранилище: {settings.runtime_data_directory}")
    print(f"Активный семестр: {active.name} (ID {active.id})")
    print(f"SQLite: {'OK' if storage.quick_check() else 'ОШИБКА'}")
    print(f"Текущий вариант недели: {kind.value}")
    for weekday in range(5):
        files = catalog.schedules_for(weekday, kind)
        names = ", ".join(path.name for path in files) if files else "НЕТ"
        print(f"{WEEKDAY_NAMES_RU[weekday]}: {names}")
    print(f"Карт: {len(catalog.maps())}")


def main() -> int:
    args = _parser().parse_args()
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=logging.INFO,
    )
    # httpx request URLs contain the Telegram bot token.  Keep transport logs
    # below WARNING so the secret never appears in the console.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = Settings.load()
        if args.healthcheck:
            from datetime import datetime, timedelta, timezone

            today = datetime.now(
                timezone(timedelta(hours=settings.utc_offset_hours))
            ).date()
            storage = BotStorage(
                settings.runtime_data_directory,
                settings.schedules_directory,
                settings.maps_directory,
                settings.academic_year_start_month,
                settings.academic_year_start_day,
            )
            if not storage.database_path.is_file():
                print("Ошибка: база данных ещё не создана", file=sys.stderr)
                return 1
            if not storage.quick_check():
                print("Ошибка: SQLite quick_check завершился неуспешно", file=sys.stderr)
                return 1
            storage.active_semester()
            print("OK")
        elif args.check:
            _print_check(settings)
        else:
            # Telegram remains an optional import for the local --check command.
            from .bot import run

            run(settings)
    except ConfigurationError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        return 2
    except ModuleNotFoundError as exc:
        if exc.name == "telegram":
            print(
                "Не установлена библиотека python-telegram-bot. "
                "Сначала запустите setup.bat.",
                file=sys.stderr,
            )
            return 3
        raise
    except KeyboardInterrupt:
        print("Бот остановлен.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
