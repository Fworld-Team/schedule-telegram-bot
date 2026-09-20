"""Persistent semester, week-correction, and uploaded-media storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Literal
from uuid import uuid4

from .calendar import (
    WeekKind,
    academic_year_anchor,
    alternating_week_kind,
    week_monday,
)
from .media import (
    IMAGE_EXTENSIONS,
    MediaCatalog,
    image_extension,
    schedule_slot_files,
)


SemesterStatus = Literal["draft", "active", "archived"]
ScheduleVariant = Literal["common", "upper", "lower"]
AssessmentType = Literal["credit", "differentiated_credit", "exam"]
DAY_SLUGS = ("monday", "tuesday", "wednesday", "thursday", "friday")
ASSESSMENT_TYPES = frozenset({"credit", "differentiated_credit", "exam"})
ASSESSMENT_TYPE_LABELS: dict[AssessmentType, str] = {
    "credit": "Зачёт",
    "differentiated_credit": "Диф. зачёт",
    "exam": "Экзамен",
}
MAX_SUBJECT_NAME_LENGTH = 120


@dataclass(frozen=True, slots=True)
class Semester:
    id: int
    name: str
    status: SemesterStatus
    anchor_monday: date
    anchor_kind: WeekKind
    created_by: int
    created_at: str


@dataclass(frozen=True, slots=True)
class SemesterAssessment:
    id: int
    semester_id: int
    subject_name: str
    assessment_type: AssessmentType
    has_coursework: bool
    created_at: str
    updated_at: str


class StorageError(RuntimeError):
    """Raised when an administrator asks for an invalid storage operation."""


class _ClosingConnection(sqlite3.Connection):
    """Commit or roll back like sqlite3.Connection, then release Windows locks."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class BotStorage:
    """A small SQLite repository with media stored on the same persistent disk."""

    def __init__(
        self,
        data_directory: Path,
        seed_schedules_directory: Path,
        seed_maps_directory: Path,
        academic_year_start_month: int = 9,
        academic_year_start_day: int = 1,
    ) -> None:
        self.data_directory = data_directory.resolve()
        self.database_path = self.data_directory / "bot.sqlite3"
        self.semesters_directory = self.data_directory / "semesters"
        self.maps_directory = self.data_directory / "maps"
        self.uploads_directory = self.data_directory / "uploads"
        self.archive_directory = self.data_directory / "archive"
        self.seed_schedules_directory = seed_schedules_directory.resolve()
        self.seed_maps_directory = seed_maps_directory.resolve()
        self.academic_year_start_month = academic_year_start_month
        self.academic_year_start_day = academic_year_start_day

    def initialize(self, today: date | None = None) -> None:
        """Create the schema and import packaged media on the very first run."""

        for directory in (
            self.data_directory,
            self.semesters_directory,
            self.maps_directory,
            self.uploads_directory,
            self.archive_directory,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS semesters (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('draft', 'active', 'archived')),
                    anchor_monday TEXT NOT NULL,
                    anchor_kind TEXT NOT NULL
                        CHECK(anchor_kind IN ('upper', 'lower')),
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    activated_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_semester
                    ON semesters(status) WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS week_overrides (
                    id INTEGER PRIMARY KEY,
                    semester_id INTEGER NOT NULL REFERENCES semesters(id),
                    effective_monday TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('upper', 'lower')),
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(semester_id, effective_monday)
                );

                CREATE TABLE IF NOT EXISTS admin_events (
                    id INTEGER PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS active_chats (
                    chat_id INTEGER PRIMARY KEY,
                    activated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS semester_assessments (
                    id INTEGER PRIMARY KEY,
                    semester_id INTEGER NOT NULL REFERENCES semesters(id),
                    subject_name TEXT NOT NULL,
                    assessment_type TEXT NOT NULL CHECK(
                        assessment_type IN (
                            'credit', 'differentiated_credit', 'exam'
                        )
                    ),
                    has_coursework INTEGER NOT NULL DEFAULT 0
                        CHECK(has_coursework IN (0, 1)),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(semester_id, subject_name)
                );
                CREATE INDEX IF NOT EXISTS assessments_by_semester
                    ON semester_assessments(semester_id);
                """
            )
            row = connection.execute("SELECT COUNT(*) FROM semesters").fetchone()
            is_first_run = row[0] == 0
            if is_first_run:
                current = today or date.today()
                anchor = academic_year_anchor(
                    current,
                    self.academic_year_start_month,
                    self.academic_year_start_day,
                )
                cursor = connection.execute(
                    """
                    INSERT INTO semesters(
                        name, status, anchor_monday, anchor_kind,
                        created_by, created_at, activated_at
                    ) VALUES (?, 'active', ?, 'upper', 0, ?, ?)
                    """,
                    (
                        "Импортированное расписание",
                        anchor.isoformat(),
                        _utc_now(),
                        _utc_now(),
                    ),
                )
                semester_id = int(cursor.lastrowid)
                target = self.schedule_directory(semester_id)
                target.mkdir(parents=True, exist_ok=True)
                _copy_supported_images(self.seed_schedules_directory, target)
                _copy_supported_images(self.seed_maps_directory, self.maps_directory)
            else:
                semester_id = 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def active_chat_ids(self) -> set[int]:
        """Return chats that should currently accept bot commands."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT chat_id FROM active_chats"
            ).fetchall()
        return {int(row["chat_id"]) for row in rows}

    def set_chat_active(self, chat_id: int, active: bool) -> None:
        """Persist whether one Telegram chat is active."""

        with self._connect() as connection:
            if active:
                connection.execute(
                    """
                    INSERT INTO active_chats(chat_id, activated_at)
                    VALUES (?, ?)
                    ON CONFLICT(chat_id)
                    DO UPDATE SET activated_at = excluded.activated_at
                    """,
                    (chat_id, _utc_now()),
                )
            else:
                connection.execute(
                    "DELETE FROM active_chats WHERE chat_id = ?",
                    (chat_id,),
                )

    def list_semesters(self) -> list[Semester]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, status, anchor_monday, anchor_kind,
                       created_by, created_at
                FROM semesters
                ORDER BY CASE status
                    WHEN 'active' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END,
                    id DESC
                """
            ).fetchall()
        return [_semester_from_row(row) for row in rows]

    def get_semester(self, semester_id: int) -> Semester | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, status, anchor_monday, anchor_kind,
                       created_by, created_at
                FROM semesters WHERE id = ?
                """,
                (semester_id,),
            ).fetchone()
        return _semester_from_row(row) if row is not None else None

    def active_semester(self) -> Semester:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, status, anchor_monday, anchor_kind,
                       created_by, created_at
                FROM semesters WHERE status = 'active'
                """
            ).fetchone()
        if row is None:
            raise StorageError("Не найден активный семестр")
        return _semester_from_row(row)

    def list_semester_assessments(
        self,
        semester_id: int,
    ) -> list[SemesterAssessment]:
        """Return one semester's assessments in case-insensitive name order."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, semester_id, subject_name, assessment_type,
                       has_coursework, created_at, updated_at
                FROM semester_assessments
                WHERE semester_id = ?
                """,
                (semester_id,),
            ).fetchall()
        assessments = [_assessment_from_row(row) for row in rows]
        return sorted(
            assessments,
            key=lambda item: item.subject_name.casefold(),
        )

    def get_semester_assessment(
        self,
        semester_id: int,
        assessment_id: int,
    ) -> SemesterAssessment | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, semester_id, subject_name, assessment_type,
                       has_coursework, created_at, updated_at
                FROM semester_assessments
                WHERE semester_id = ? AND id = ?
                """,
                (semester_id, assessment_id),
            ).fetchone()
        return _assessment_from_row(row) if row is not None else None

    def create_semester_assessment(
        self,
        semester_id: int,
        subject_name: str,
        assessment_type: AssessmentType,
        has_coursework: bool,
        user_id: int,
    ) -> SemesterAssessment:
        cleaned = _validate_assessment(
            subject_name,
            assessment_type,
            has_coursework,
        )
        now = _utc_now()
        try:
            with self._connect() as connection:
                _ensure_semester_exists(connection, semester_id)
                _ensure_unique_subject(connection, semester_id, cleaned)
                cursor = connection.execute(
                    """
                    INSERT INTO semester_assessments(
                        semester_id, subject_name, assessment_type,
                        has_coursework, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        semester_id,
                        cleaned,
                        assessment_type,
                        int(has_coursework),
                        now,
                        now,
                    ),
                )
                assessment_id = int(cursor.lastrowid)
                self._event(
                    connection,
                    user_id,
                    "assessment_created",
                    f"{semester_id}:{assessment_id}",
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(
                "Такая дисциплина уже есть в этом семестре"
            ) from exc
        assessment = self.get_semester_assessment(semester_id, assessment_id)
        if assessment is None:
            raise StorageError("Не удалось прочитать созданную дисциплину")
        return assessment

    def update_semester_assessment(
        self,
        semester_id: int,
        assessment_id: int,
        subject_name: str,
        assessment_type: AssessmentType,
        has_coursework: bool,
        user_id: int,
    ) -> SemesterAssessment:
        cleaned = _validate_assessment(
            subject_name,
            assessment_type,
            has_coursework,
        )
        try:
            with self._connect() as connection:
                _ensure_semester_exists(connection, semester_id)
                existing = connection.execute(
                    """
                    SELECT id FROM semester_assessments
                    WHERE semester_id = ? AND id = ?
                    """,
                    (semester_id, assessment_id),
                ).fetchone()
                if existing is None:
                    raise StorageError("Дисциплина не найдена")
                _ensure_unique_subject(
                    connection,
                    semester_id,
                    cleaned,
                    exclude_assessment_id=assessment_id,
                )
                connection.execute(
                    """
                    UPDATE semester_assessments
                    SET subject_name = ?, assessment_type = ?,
                        has_coursework = ?, updated_at = ?
                    WHERE semester_id = ? AND id = ?
                    """,
                    (
                        cleaned,
                        assessment_type,
                        int(has_coursework),
                        _utc_now(),
                        semester_id,
                        assessment_id,
                    ),
                )
                self._event(
                    connection,
                    user_id,
                    "assessment_updated",
                    f"{semester_id}:{assessment_id}",
                )
        except sqlite3.IntegrityError as exc:
            raise StorageError(
                "Такая дисциплина уже есть в этом семестре"
            ) from exc
        assessment = self.get_semester_assessment(semester_id, assessment_id)
        if assessment is None:
            raise StorageError("Не удалось прочитать обновлённую дисциплину")
        return assessment

    def delete_semester_assessment(
        self,
        semester_id: int,
        assessment_id: int,
        user_id: int,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM semester_assessments
                WHERE semester_id = ? AND id = ?
                """,
                (semester_id, assessment_id),
            )
            removed = cursor.rowcount > 0
            if removed:
                self._event(
                    connection,
                    user_id,
                    "assessment_deleted",
                    f"{semester_id}:{assessment_id}",
                )
        return removed

    def create_semester(
        self,
        name: str,
        reference_date: date,
        reference_kind: WeekKind,
        user_id: int,
    ) -> Semester:
        cleaned = " ".join(name.split())
        if not cleaned or len(cleaned) > 80:
            raise StorageError("Название должно содержать от 1 до 80 символов")
        anchor = week_monday(reference_date)
        now = _utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO semesters(
                    name, status, anchor_monday, anchor_kind, created_by, created_at
                ) VALUES (?, 'draft', ?, ?, ?, ?)
                """,
                (cleaned, anchor.isoformat(), reference_kind.value, user_id, now),
            )
            semester_id = int(cursor.lastrowid)
            self._event(connection, user_id, "semester_created", str(semester_id))
        self.schedule_directory(semester_id).mkdir(parents=True, exist_ok=True)
        semester = self.get_semester(semester_id)
        if semester is None:
            raise StorageError("Не удалось прочитать созданный семестр")
        return semester

    def activate_semester(self, semester_id: int, user_id: int) -> Semester:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id FROM semesters WHERE id = ?", (semester_id,)
            ).fetchone()
            if row is None:
                raise StorageError("Семестр не найден")
            connection.execute(
                """
                UPDATE semesters SET status = 'archived'
                WHERE status = 'active' AND id <> ?
                """,
                (semester_id,),
            )
            connection.execute(
                """
                UPDATE semesters
                SET status = 'active', activated_at = ?
                WHERE id = ?
                """,
                (now, semester_id),
            )
            self._event(connection, user_id, "semester_activated", str(semester_id))
        return self.active_semester()

    def week_kind(self, target: date) -> WeekKind:
        semester = self.active_semester()
        anchor_date = semester.anchor_monday
        anchor_kind = semester.anchor_kind
        monday = week_monday(target)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT effective_monday, kind
                FROM week_overrides
                WHERE semester_id = ? AND effective_monday <= ?
                ORDER BY effective_monday DESC LIMIT 1
                """,
                (semester.id, monday.isoformat()),
            ).fetchone()
        if row is not None:
            anchor_date = date.fromisoformat(row["effective_monday"])
            anchor_kind = WeekKind(row["kind"])
        return alternating_week_kind(target, anchor_date, anchor_kind)

    def week_source(self, target: date) -> tuple[date, WeekKind, bool]:
        """Return the anchor used for target and whether it is an override."""

        semester = self.active_semester()
        monday = week_monday(target)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT effective_monday, kind
                FROM week_overrides
                WHERE semester_id = ? AND effective_monday <= ?
                ORDER BY effective_monday DESC LIMIT 1
                """,
                (semester.id, monday.isoformat()),
            ).fetchone()
        if row is None:
            return semester.anchor_monday, semester.anchor_kind, False
        return date.fromisoformat(row["effective_monday"]), WeekKind(row["kind"]), True

    def set_current_week_kind(
        self,
        current_date: date,
        kind: WeekKind,
        user_id: int,
    ) -> None:
        semester = self.active_semester()
        monday = week_monday(current_date)
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO week_overrides(
                    semester_id, effective_monday, kind, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(semester_id, effective_monday)
                DO UPDATE SET kind = excluded.kind,
                              created_by = excluded.created_by,
                              created_at = excluded.created_at
                """,
                (semester.id, monday.isoformat(), kind.value, user_id, now),
            )
            self._event(
                connection,
                user_id,
                "week_corrected",
                f"{semester.id}:{monday.isoformat()}:{kind.value}",
            )

    def clear_current_week_override(self, current_date: date, user_id: int) -> bool:
        semester = self.active_semester()
        monday = week_monday(current_date)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM week_overrides
                WHERE semester_id = ? AND effective_monday = ?
                """,
                (semester.id, monday.isoformat()),
            )
            removed = cursor.rowcount > 0
            if removed:
                self._event(
                    connection,
                    user_id,
                    "week_correction_removed",
                    f"{semester.id}:{monday.isoformat()}",
                )
        return removed

    def semester_week_number(self, target: date) -> int | None:
        semester = self.active_semester()
        number = ((week_monday(target) - semester.anchor_monday).days // 7) + 1
        return number if number > 0 else None

    def schedule_directory(self, semester_id: int) -> Path:
        return self.semesters_directory / str(semester_id) / "schedules"

    def active_catalog(self) -> MediaCatalog:
        semester = self.active_semester()
        return MediaCatalog(self.schedule_directory(semester.id), self.maps_directory)

    def coverage(self, semester_id: int) -> dict[tuple[int, WeekKind], int]:
        catalog = MediaCatalog(self.schedule_directory(semester_id), self.maps_directory)
        return {
            (weekday, kind): len(catalog.schedules_for(weekday, kind))
            for weekday in range(5)
            for kind in (WeekKind.UPPER, WeekKind.LOWER)
        }

    def new_upload_path(self) -> Path:
        self.uploads_directory.mkdir(parents=True, exist_ok=True)
        return self.uploads_directory / f"{uuid4().hex}.part"

    def install_schedule_image(
        self,
        uploaded_path: Path,
        semester_id: int,
        weekday: int,
        variant: ScheduleVariant,
        user_id: int,
    ) -> Path:
        if self.get_semester(semester_id) is None:
            raise StorageError("Семестр не найден")
        if not 0 <= weekday <= 4:
            raise StorageError("Неверный день недели")
        if variant not in {"common", "upper", "lower"}:
            raise StorageError("Неверный тип расписания")
        extension = image_extension(uploaded_path)
        if extension is None:
            raise StorageError("Поддерживаются только настоящие PNG, JPEG и WebP")

        directory = self.schedule_directory(semester_id)
        directory.mkdir(parents=True, exist_ok=True)
        base = DAY_SLUGS[weekday]
        if variant != "common":
            base += f"_{variant}"
        target = directory / f"{base}{extension}"
        variants_to_replace = (
            ("common", "upper", "lower")
            if variant == "common"
            else (variant,)
        )
        existing = sorted(
            {
                path
                for current_variant in variants_to_replace
                for path in schedule_slot_files(
                    directory,
                    weekday,
                    current_variant,
                )
            }
        )
        self._backup(existing, f"semester-{semester_id}")
        os.replace(uploaded_path, target)
        for old in existing:
            if old != target:
                old.unlink(missing_ok=True)
        with self._connect() as connection:
            self._event(
                connection,
                user_id,
                "schedule_uploaded",
                f"{semester_id}:{weekday}:{variant}:{target.name}",
            )
        return target

    def install_map_image(self, uploaded_path: Path, user_id: int) -> Path:
        extension = image_extension(uploaded_path)
        if extension is None:
            raise StorageError("Поддерживаются только настоящие PNG, JPEG и WebP")
        self.maps_directory.mkdir(parents=True, exist_ok=True)
        existing = MediaCatalog(
            self.data_directory / "_unused",
            self.maps_directory,
        ).maps()
        self._backup(existing, "maps")
        target = self.maps_directory / f"campus{extension}"
        os.replace(uploaded_path, target)
        for old in existing:
            if old != target:
                old.unlink(missing_ok=True)
        with self._connect() as connection:
            self._event(connection, user_id, "map_uploaded", target.name)
        return target

    def quick_check(self) -> bool:
        with self._connect() as connection:
            result = connection.execute("PRAGMA quick_check").fetchone()[0]
        return result == "ok"

    def _backup(self, paths: list[Path], category: str) -> None:
        if not paths:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        target = self.archive_directory / category / f"{stamp}-{uuid4().hex[:8]}"
        target.mkdir(parents=True, exist_ok=False)
        for path in paths:
            shutil.copy2(path, target / path.name)
        revisions = sorted(
            (path for path in target.parent.iterdir() if path.is_dir()),
            key=lambda path: path.name,
            reverse=True,
        )
        for old_revision in revisions[20:]:
            shutil.rmtree(old_revision)

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        user_id: int,
        action: str,
        details: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO admin_events(user_id, action, details, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, action, details, _utc_now()),
        )


def _semester_from_row(row: sqlite3.Row) -> Semester:
    return Semester(
        id=int(row["id"]),
        name=str(row["name"]),
        status=row["status"],
        anchor_monday=date.fromisoformat(row["anchor_monday"]),
        anchor_kind=WeekKind(row["anchor_kind"]),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
    )


def _assessment_from_row(row: sqlite3.Row) -> SemesterAssessment:
    return SemesterAssessment(
        id=int(row["id"]),
        semester_id=int(row["semester_id"]),
        subject_name=str(row["subject_name"]),
        assessment_type=row["assessment_type"],
        has_coursework=bool(row["has_coursework"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _validate_assessment(
    subject_name: str,
    assessment_type: str,
    has_coursework: bool,
) -> str:
    cleaned = " ".join(subject_name.split())
    if not cleaned or len(cleaned) > MAX_SUBJECT_NAME_LENGTH:
        raise StorageError(
            "Название дисциплины должно содержать от 1 до "
            f"{MAX_SUBJECT_NAME_LENGTH} символов"
        )
    if assessment_type not in ASSESSMENT_TYPES:
        raise StorageError("Неверный вид аттестации")
    if not isinstance(has_coursework, bool):
        raise StorageError("Признак курсовой работы должен быть Да или Нет")
    return cleaned


def _ensure_semester_exists(
    connection: sqlite3.Connection,
    semester_id: int,
) -> None:
    row = connection.execute(
        "SELECT id FROM semesters WHERE id = ?",
        (semester_id,),
    ).fetchone()
    if row is None:
        raise StorageError("Семестр не найден")


def _ensure_unique_subject(
    connection: sqlite3.Connection,
    semester_id: int,
    subject_name: str,
    *,
    exclude_assessment_id: int | None = None,
) -> None:
    rows = connection.execute(
        """
        SELECT id, subject_name FROM semester_assessments
        WHERE semester_id = ?
        """,
        (semester_id,),
    ).fetchall()
    folded = subject_name.casefold()
    for row in rows:
        if exclude_assessment_id == int(row["id"]):
            continue
        if str(row["subject_name"]).casefold() == folded:
            raise StorageError("Такая дисциплина уже есть в этом семестре")


def _copy_supported_images(source: Path, target: Path) -> None:
    try:
        candidates = list(source.iterdir())
    except OSError:
        return
    for path in candidates:
        if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS:
            if image_extension(path) is not None:
                destination = target / path.name
                if not destination.exists():
                    shutil.copy2(path, destination)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
