"""Discover locally stored schedule and map images on every request."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
import re
import warnings

from PIL import Image, UnidentifiedImageError

from .calendar import WeekKind


IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
ALLOWED_IMAGE_FORMATS = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "WEBP": ".webp",
}
MAX_IMAGE_PIXELS = 40_000_000
LOGGER = logging.getLogger(__name__)

WEEKDAY_SLUGS: dict[int, tuple[str, ...]] = {
    0: ("monday", "понедельник", "пн"),
    1: ("tuesday", "вторник", "вт"),
    2: ("wednesday", "среда", "ср"),
    3: ("thursday", "четверг", "чт"),
    4: ("friday", "пятница", "пт"),
}

WEEK_VARIANTS: dict[WeekKind, tuple[str, ...]] = {
    WeekKind.UPPER: ("upper", "odd", "верхняя", "нечетная"),
    WeekKind.LOWER: ("lower", "even", "нижняя", "четная"),
}


class MediaCatalog:
    """Resolve images by canonical filename without caching directory contents."""

    def __init__(self, schedules_directory: Path, maps_directory: Path) -> None:
        self.schedules_directory = schedules_directory
        self.maps_directory = maps_directory

    def schedules_for(self, weekday: int, week_kind: WeekKind) -> list[Path]:
        """Return week-specific images, falling back to generic day images."""

        aliases = WEEKDAY_SLUGS.get(weekday)
        if aliases is None:
            return []

        files = _image_files(self.schedules_directory)
        variants = WEEK_VARIANTS[week_kind]
        specific = [
            path
            for path in files
            if _matches_specific(path.stem, aliases, variants)
        ]
        if specific:
            return specific

        return [path for path in files if _matches_generic(path.stem, aliases)]

    def maps(self) -> list[Path]:
        """Return every supported image from the maps directory."""

        return _image_files(self.maps_directory)


def schedule_slot_files(
    directory: Path,
    weekday: int,
    variant: str,
) -> list[Path]:
    """Return every file belonging to one editable administrator slot."""

    aliases = WEEKDAY_SLUGS.get(weekday)
    if aliases is None:
        return []
    files = _image_files(directory)
    if variant == "common":
        return [path for path in files if _matches_generic(path.stem, aliases)]
    try:
        kind = WeekKind(variant)
    except ValueError:
        return []
    variants = WEEK_VARIANTS[kind]
    return [
        path
        for path in files
        if _matches_specific(path.stem, aliases, variants)
    ]


def _image_files(directory: Path) -> list[Path]:
    try:
        files = [
            path
            for path in directory.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.casefold() in IMAGE_EXTENSIONS
            and _has_image_signature(path)
        ]
    except OSError:
        LOGGER.warning("Не удалось прочитать каталог изображений %s", directory, exc_info=True)
        return []
    return sorted(files, key=_natural_sort_key)


def image_extension(path: Path) -> str | None:
    """Decode and validate an image, with a cache invalidated by file changes."""

    try:
        stat = path.stat()
    except OSError:
        LOGGER.warning("Не удалось проверить изображение %s", path, exc_info=True)
        return None
    return _validated_image_extension(
        str(path.resolve()),
        stat.st_size,
        stat.st_mtime_ns,
    )


@lru_cache(maxsize=512)
def _validated_image_extension(
    path_value: str,
    size: int,
    modified_ns: int,
) -> str | None:
    """Validate one immutable file revision; size/mtime form the cache key."""

    del size, modified_ns
    path = Path(path_value)
    try:
        with path.open("rb") as image_file:
            header = image_file.read(12)
    except OSError:
        return None

    has_supported_magic = (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or (
            len(header) >= 12
            and header[:4] == b"RIFF"
            and header[8:12] == b"WEBP"
        )
    )
    if not has_supported_magic:
        return None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                extension = ALLOWED_IMAGE_FORMATS.get(image.format or "")
                width, height = image.size
                if (
                    extension is None
                    or width <= 0
                    or height <= 0
                    or width * height > MAX_IMAGE_PIXELS
                    or getattr(image, "n_frames", 1) != 1
                ):
                    return None
                image.verify()
            # verify() does not decode pixels. Reopen and load to catch damaged
            # compressed image data before it replaces a working schedule.
            with Image.open(path) as image:
                image.load()
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        OSError,
        UnidentifiedImageError,
        ValueError,
    ):
        return None
    return extension


def _has_image_signature(path: Path) -> bool:
    """Reject empty and obviously invalid files before Telegram sees them."""

    if image_extension(path) is None:
        LOGGER.warning("Файл пропущен: неверный формат изображения: %s", path)
        return False
    return True


def _normalise(value: str) -> str:
    return value.casefold().replace("ё", "е")


def _matches_generic(stem: str, aliases: tuple[str, ...]) -> bool:
    normalised = _normalise(stem)
    return any(
        _fullmatch_parts(normalised, (_normalise(alias),), numbered=True)
        for alias in aliases
    )


def _matches_specific(
    stem: str,
    aliases: tuple[str, ...],
    variants: tuple[str, ...],
) -> bool:
    normalised = _normalise(stem)
    return any(
        _fullmatch_parts(
            normalised,
            (_normalise(alias), _normalise(variant)),
            numbered=True,
        )
        for alias in aliases
        for variant in variants
    )


def _fullmatch_parts(value: str, parts: tuple[str, ...], numbered: bool) -> bool:
    separator = r"[\s._-]+"
    pattern = separator.join(re.escape(part) for part in parts)
    if numbered:
        pattern += rf"(?:{separator}\d+)?"
    return re.fullmatch(pattern, value) is not None


def _natural_sort_key(path: Path) -> tuple[tuple[int, object], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", path.name.casefold())
    )
