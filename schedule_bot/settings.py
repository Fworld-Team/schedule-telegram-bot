"""Project configuration and secret-token loading."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOKEN_PATTERN = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")


class ConfigurationError(RuntimeError):
    """Raised when local bot configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated application settings."""

    project_root: Path
    bot_title: str
    schedules_directory: Path
    maps_directory: Path
    token_file: Path
    academic_year_start_month: int
    academic_year_start_day: int
    utc_offset_hours: int
    data_directory: Path | None = None
    admin_user_ids: frozenset[int] = frozenset()

    @classmethod
    def load(cls, project_root: Path | None = None) -> "Settings":
        root = (project_root or PROJECT_ROOT).resolve()
        config_path = root / "config.json"
        if not config_path.is_file():
            raise ConfigurationError(f"Не найден файл настроек: {config_path}")

        try:
            raw: Any = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(
                f"Не удалось прочитать config.json: {exc}"
            ) from exc

        if not isinstance(raw, dict):
            raise ConfigurationError("Корень config.json должен быть объектом")

        required = {
            "bot_title",
            "schedules_directory",
            "maps_directory",
            "token_file",
            "academic_year_start",
            "utc_offset_hours",
        }
        missing = sorted(required - raw.keys())
        if missing:
            raise ConfigurationError(
                "В config.json отсутствуют поля: " + ", ".join(missing)
            )

        title = raw["bot_title"]
        if not isinstance(title, str) or not title.strip():
            raise ConfigurationError("bot_title должен быть непустой строкой")

        month, day = _parse_month_day(raw["academic_year_start"])

        offset = raw["utc_offset_hours"]
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ConfigurationError("utc_offset_hours должен быть целым числом")
        if not -12 <= offset <= 14:
            raise ConfigurationError("utc_offset_hours должен быть от -12 до 14")

        data_directory = _resolve_path(
            root,
            os.environ.get("BOT_DATA_DIR") or raw.get("data_directory", "data"),
        )
        admin_user_ids = _parse_admin_ids(
            os.environ.get("ADMIN_USER_IDS")
            or os.environ.get("TELEGRAM_ADMIN_IDS")
            or raw.get("admin_user_ids", [])
        )

        return cls(
            project_root=root,
            bot_title=title.strip(),
            schedules_directory=_resolve_path(root, raw["schedules_directory"]),
            maps_directory=_resolve_path(root, raw["maps_directory"]),
            token_file=_resolve_path(root, raw["token_file"]),
            academic_year_start_month=month,
            academic_year_start_day=day,
            utc_offset_hours=offset,
            data_directory=data_directory,
            admin_user_ids=admin_user_ids,
        )

    def ensure_media_directories(self) -> None:
        self.schedules_directory.mkdir(parents=True, exist_ok=True)
        self.maps_directory.mkdir(parents=True, exist_ok=True)
        self.runtime_data_directory.mkdir(parents=True, exist_ok=True)

    @property
    def runtime_data_directory(self) -> Path:
        """Writable persistent directory, overridable for a server volume."""

        return self.data_directory or (self.project_root / "data")


def load_bot_token(settings: Settings) -> str:
    """Load a token from the environment first, then from token.txt."""

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    token_file_override = os.environ.get("TELEGRAM_BOT_TOKEN_FILE", "").strip()
    token_file = Path(token_file_override) if token_file_override else settings.token_file
    if not token:
        try:
            token = token_file.read_text(encoding="utf-8-sig").strip()
        except FileNotFoundError as exc:
            raise ConfigurationError(
                "Не найден токен. Создайте token.txt по образцу token.example.txt "
                "или задайте переменную TELEGRAM_BOT_TOKEN."
            ) from exc
        except OSError as exc:
            raise ConfigurationError("Не удалось прочитать файл с токеном") from exc

    if token == "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE" or not TOKEN_PATTERN.fullmatch(
        token
    ):
        raise ConfigurationError(
            "Токен Telegram не заполнен или имеет неверный формат. "
            "Получите его у @BotFather и вставьте одной строкой в token.txt."
        )
    return token


def _resolve_path(root: Path, raw_value: Any) -> Path:
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise ConfigurationError("Пути в config.json должны быть непустыми строками")
    path = Path(raw_value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _parse_month_day(raw_value: Any) -> tuple[int, int]:
    if not isinstance(raw_value, str) or not re.fullmatch(r"\d{2}-\d{2}", raw_value):
        raise ConfigurationError("academic_year_start должен иметь формат MM-DD")
    month, day = (int(part) for part in raw_value.split("-"))
    try:
        # A recurring month/day must exist in every academic year.
        date(2023, month, day)
    except ValueError as exc:
        raise ConfigurationError("academic_year_start содержит неверную дату") from exc
    return month, day


def _parse_admin_ids(raw_value: Any) -> frozenset[int]:
    """Parse immutable numeric Telegram user IDs from JSON or an env string."""

    if raw_value in (None, "", []):
        return frozenset()
    values: list[Any]
    if isinstance(raw_value, str):
        values = [part.strip() for part in raw_value.split(",") if part.strip()]
    elif isinstance(raw_value, list):
        values = raw_value
    else:
        raise ConfigurationError("admin_user_ids должен быть списком чисел")

    result: set[int] = set()
    for value in values:
        if isinstance(value, bool):
            raise ConfigurationError("admin_user_ids должен содержать только числа")
        try:
            user_id = int(value)
        except (TypeError, ValueError) as exc:
            raise ConfigurationError(
                "admin_user_ids должен содержать только числовые Telegram ID"
            ) from exc
        if user_id <= 0:
            raise ConfigurationError("Telegram ID администратора должен быть больше нуля")
        result.add(user_id)
    return frozenset(result)
