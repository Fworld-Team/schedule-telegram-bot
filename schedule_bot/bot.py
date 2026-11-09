"""Telegram handlers and polling entry point."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Final

import httpx
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, NetworkError, TelegramError
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from .admin import build_admin_conversation, my_id
from .calendar import (
    WEEKDAY_NAMES_RU,
    WeekKind,
    academic_week_kind,
    academic_week_number,
    date_in_current_week,
)
from .media import MediaCatalog
from .schedule_monitor import start_monitor, stop_monitor
from .settings import Settings, load_bot_token
from .storage import ASSESSMENT_TYPE_LABELS, BotStorage


LOGGER = logging.getLogger(__name__)

TODAY_BUTTON: Final = "📅 Сегодня"
STUDY_PLAN_BUTTON: Final = "📚 Учебный план"
ASSESSMENTS_BUTTON: Final = "🎓 Экзамены"
MAP_BUTTON: Final = "🗺️ Карта"
HELP_BUTTON: Final = "ℹ️ Помощь"
EXIT_BUTTON: Final = "🚪 Выход"
STUDY_PLAN_RELATIVE_PATH: Final = (
    Path("media") / "study_plan" / "study_plan_2026_2027.png"
)
ACTIVE_CHAT_IDS_KEY: Final = "active_chat_ids"
POLLING_WATCHDOG_TASK_KEY: Final = "polling_watchdog_task"
SEND_ATTEMPTS: Final = 3
SEND_CONNECT_TIMEOUT: Final = 20.0
SEND_READ_TIMEOUT: Final = 30.0
SEND_WRITE_TIMEOUT: Final = 30.0
POLLING_WATCHDOG_INTERVAL_SECONDS: Final = 10.0
POLLING_WATCHDOG_BACKLOG_LIMIT: Final = 2
POLLING_WATCHDOG_API_ERROR_LIMIT: Final = 3


DAY_BUTTONS: Final[dict[str, int]] = {
    "Пн": 0,
    "Вт": 1,
    "Ср": 2,
    "Чт": 3,
    "Пт": 4,
}

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [
        [TODAY_BUTTON, "Пн", "Вт"],
        ["Ср", "Чт", "Пт"],
        [STUDY_PLAN_BUTTON, ASSESSMENTS_BUTTON],
        [MAP_BUTTON, HELP_BUTTON, EXIT_BUTTON],
    ],
    resize_keyboard=True,
    is_persistent=True,
    input_field_placeholder="Выберите день",
)

WEEK_KIND_LABELS: Final[dict[WeekKind, str]] = {
    WeekKind.UPPER: "верхняя",
    WeekKind.LOWER: "нижняя",
}


def build_application(settings: Settings) -> Application:
    """Create an application with all commands and text-button handlers."""

    storage = BotStorage(
        settings.runtime_data_directory,
        settings.schedules_directory,
        settings.maps_directory,
        settings.academic_year_start_month,
        settings.academic_year_start_day,
    )
    storage.initialize(_today(settings))
    builder = (
        ApplicationBuilder()
        .token(load_bot_token(settings))
        .concurrent_updates(False)
    )
    if settings.proxy_url:
        # Polling and ordinary Bot API requests use separate HTTP clients.
        # XHTTP tunnels can leave a reused CONNECT stream half-open. Polling is
        # low-volume, so a fresh proxy stream for every long-poll request is a
        # safer trade-off than keeping one stale connection indefinitely.
        polling_request = HTTPXRequest(
            connection_pool_size=2,
            proxy=settings.proxy_url,
            httpx_kwargs={
                "limits": httpx.Limits(
                    max_connections=2,
                    max_keepalive_connections=0,
                )
            },
        )
        builder.proxy(settings.proxy_url).get_updates_request(polling_request)
    application = builder.build()
    application.bot_data["settings"] = settings
    application.bot_data["storage"] = storage
    application.bot_data["catalog"] = storage.active_catalog()
    application.bot_data[ACTIVE_CHAT_IDS_KEY] = storage.active_chat_ids()

    application.add_handler(TypeHandler(Update, activity_gate), group=-1)
    application.add_handler(build_admin_conversation())
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", start))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("today", today_command))
    application.add_handler(CommandHandler("map", map_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("myid", my_id))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, route_text))
    application.add_error_handler(on_error)
    application.post_init = initialize_application
    application.post_stop = stop_monitor
    application.post_shutdown = shutdown_application
    return application


async def initialize_application(application: Application) -> None:
    """Finish startup and start the independent watchdog and schedule monitor."""

    await set_commands(application)
    application.bot_data[POLLING_WATCHDOG_TASK_KEY] = asyncio.create_task(
        polling_watchdog(application),
        name="polling-watchdog",
    )
    start_monitor(application, ACTIVE_CHAT_IDS_KEY)


async def shutdown_application(application: Application) -> None:
    """Stop background work without leaving an un-awaited watchdog task."""

    task = application.bot_data.pop(POLLING_WATCHDOG_TASK_KEY, None)
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    await stop_monitor(application)


async def polling_watchdog(
    application: Application,
    *,
    interval: float = POLLING_WATCHDOG_INTERVAL_SECONDS,
    backlog_limit: int = POLLING_WATCHDOG_BACKLOG_LIMIT,
    api_error_limit: int = POLLING_WATCHDOG_API_ERROR_LIMIT,
) -> None:
    """Restart the container when Telegram updates remain uncollected."""

    backlog_checks = 0
    api_error_checks = 0
    while True:
        await asyncio.sleep(interval)
        try:
            webhook = await application.bot.get_webhook_info()
        except TelegramError as exc:
            api_error_checks += 1
            LOGGER.warning(
                "Не удалось проверить Telegram API (%s/%s): %s",
                api_error_checks,
                api_error_limit,
                exc,
            )
        else:
            api_error_checks = 0
            pending = webhook.pending_update_count
            if pending == 0:
                backlog_checks = 0
                continue

            backlog_checks += 1
            LOGGER.warning(
                "Telegram хранит %s необработанных обновлений (%s/%s)",
                pending,
                backlog_checks,
                backlog_limit,
            )
        if (
            backlog_checks >= backlog_limit
            or api_error_checks >= api_error_limit
        ):
            LOGGER.error("Связь с Telegram не восстановилась; перезапускаю приложение")
            application.stop_running()
            return


async def set_commands(application: Application) -> None:
    """Register a compact Telegram command menu; startup still works if it fails."""

    public_commands = [
        BotCommand("start", "Открыть меню"),
        BotCommand("stop", "Выключить в этом чате"),
        BotCommand("today", "Расписание на сегодня"),
        BotCommand("map", "Показать карту"),
        BotCommand("status", "Проверить локальные файлы"),
        BotCommand("help", "Помощь"),
    ]
    try:
        await application.bot.set_my_commands(public_commands)
    except TelegramError:
        LOGGER.warning("Не удалось обновить меню команд", exc_info=True)
        return

    admin_commands = [
        *public_commands,
        BotCommand("myid", "Показать мой Telegram ID"),
        BotCommand("admin", "Управление ботом"),
    ]
    settings: Settings = application.bot_data["settings"]
    for admin_user_id in settings.admin_user_ids:
        try:
            await application.bot.set_my_commands(
                admin_commands,
                scope=BotCommandScopeChat(chat_id=admin_user_id),
            )
        except TelegramError:
            LOGGER.warning(
                "Не удалось обновить меню команд администратора %s",
                admin_user_id,
                exc_info=True,
            )


async def activity_gate(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Keep an inactive chat silent until someone explicitly sends /start."""

    if _is_start_command(update) or _is_active(update, context):
        return
    if update.callback_query is not None:
        try:
            await update.callback_query.answer()
        except TelegramError:
            LOGGER.debug("Не удалось закрыть устаревшую кнопку", exc_info=True)
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    _set_active(update, context, True)
    settings = _settings(context)
    await message.reply_text(
        f"{settings.bot_title}\n\n"
        "Выберите день — я пришлю фотографию расписания. "
        "Кнопка «Сегодня» определит день и тип недели автоматически.",
        reply_markup=MAIN_KEYBOARD,
    )


async def stop_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Disable replies only in the current Telegram chat."""

    await _disable_current_chat(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        "Как пользоваться:\n"
        "• «Сегодня» — расписание на текущую дату;\n"
        "• Пн–Пт — день текущей календарной недели;\n"
        "• «Учебный план» — календарный учебный график;\n"
        "• «Экзамены» — зачёты, диф. зачёты и экзамены текущего семестра;\n"
        "• «Карта» — показывает карту;\n"
        "• «Выход» — скрывает меню, уведомления продолжают приходить;\n"
        "• /stop — отключает ответы и уведомления в этом чате.\n\n"
        "Верхняя неделя — нечётная, нижняя — чётная.\n"
        "Неделя с 1 сентября считается верхней (нечётной).\n"
        "С 8 сентября начинается нижняя (чётная) неделя.",
        reply_markup=MAIN_KEYBOARD,
    )


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_schedule(update, context, _today(_settings(context)))


async def map_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_maps(update, context)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    settings = _settings(context)
    catalog = _catalog(context)
    today = _today(settings)
    storage = _storage_or_none(context)
    kind = _week_kind(today, settings, storage)
    lines = [
        "Проверка расписания:",
        f"Текущая неделя: {WEEK_KIND_LABELS[kind]}",
    ]
    if storage is not None:
        lines.append(f"Активный семестр: {storage.active_semester().name}")
        lines.append(f"База данных: {'OK' if storage.quick_check() else 'ОШИБКА'}")
    for weekday in range(5):
        target = date_in_current_week(today, weekday)
        target_kind = _week_kind(target, settings, storage)
        files = catalog.schedules_for(weekday, target_kind)
        names = ", ".join(path.name for path in files) if files else "нет файла"
        lines.append(
            f"{WEEKDAY_NAMES_RU[weekday]} ({WEEK_KIND_LABELS[target_kind]}): {names}"
        )
    maps = catalog.maps()
    lines.append(f"Карты: {len(maps)}")
    await message.reply_text("\n".join(lines), reply_markup=MAIN_KEYBOARD)


async def route_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None or not message.text:
        return

    text = message.text.strip()
    if text in DAY_BUTTONS:
        settings = _settings(context)
        target = date_in_current_week(_today(settings), DAY_BUTTONS[text])
        await _send_schedule(update, context, target)
    elif text == TODAY_BUTTON:
        await today_command(update, context)
    elif text == STUDY_PLAN_BUTTON:
        await _send_study_plan(update, context)
    elif text == ASSESSMENTS_BUTTON:
        await _send_assessments(update, context)
    elif text == MAP_BUTTON:
        await map_command(update, context)
    elif text == HELP_BUTTON:
        await help_command(update, context)
    elif text == EXIT_BUTTON:
        await message.reply_text(
            "Меню скрыто. Чтобы открыть его снова, отправьте /start.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def _disable_current_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return
    _set_active(update, context, False)
    await message.reply_text(
        "Бот выключен в этом чате. Чтобы включить его снова, отправьте /start.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def _send_schedule(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    target_date,
) -> None:
    message = update.effective_message
    if message is None:
        return
    weekday = target_date.weekday()
    if weekday > 4:
        await message.reply_text(
            f"{WEEKDAY_NAMES_RU[weekday]}, {target_date:%d.%m.%Y}: "
            "занятия на этот день не настроены.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    settings = _settings(context)
    storage = _storage_or_none(context)
    kind = _week_kind(target_date, settings, storage)
    files = _catalog(context).schedules_for(weekday, kind)
    if not files:
        await message.reply_text(
            f"Для дня «{WEEKDAY_NAMES_RU[weekday]}» пока нет фотографии.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    week_number = (
        storage.semester_week_number(target_date)
        if storage is not None
        else academic_week_number(
            target_date,
            settings.academic_year_start_month,
            settings.academic_year_start_day,
        )
    )
    week_text = (
        f"{week_number}-я неделя от начала семестра · "
        if week_number is not None
        else ""
    )
    caption = (
        f"{WEEKDAY_NAMES_RU[weekday]}, {target_date:%d.%m.%Y}\n"
        f"{week_text}{WEEK_KIND_LABELS[kind]}"
    )
    await _send_images(message, files, caption)


async def _send_maps(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    files = _catalog(context).maps()
    if not files:
        await message.reply_text(
            "Карта пока не загружена.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    await _send_images(message, files, "Карта кампуса")


async def _send_study_plan(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return
    path = _settings(context).project_root / STUDY_PLAN_RELATIVE_PATH
    if not path.is_file():
        await message.reply_text(
            "Учебный график пока не загружен.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    caption = (
        "📚 <b>Учебный график 2026/2027</b>\n\n"
        "🎓 Зимняя сессия: <b>09.01–24.01</b>\n"
        "❄️ Зимние каникулы: <b>25.01–07.02</b>\n"
        "🎓 Летняя сессия: <b>14.06–27.06</b>\n"
        "🏢 Практика: <b>28.06–25.07</b>\n"
        "🌴 Летние каникулы: <b>26.07–31.08</b>\n\n"
        "Обозначения:\n"
        "Э — экзаменационная сессия\n"
        "П — производственная практика\n"
        "К — каникулы\n"
        "* — нерабочие праздничные дни"
    )
    await _send_images(
        message,
        [path],
        caption,
        parse_mode="HTML",
    )


async def _send_assessments(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    message = update.effective_message
    if message is None:
        return
    storage = _storage_or_none(context)
    assessments = []
    if storage is not None:
        semester = storage.active_semester()
        assessments = storage.list_semester_assessments(semester.id)
    if not assessments:
        await message.reply_text(
            "Информация об экзаменах и зачётах на текущий семестр пока "
            "не добавлена.",
            reply_markup=MAIN_KEYBOARD,
        )
        return
    lines = ["🎓 Экзамены и зачёты"]
    for index, assessment in enumerate(assessments, start=1):
        suffix = " + курсовая" if assessment.has_coursework else ""
        lines.append(
            f"{index}. {assessment.subject_name} — "
            f"{ASSESSMENT_TYPE_LABELS[assessment.assessment_type]}{suffix}"
        )
    await message.reply_text(
        "\n\n".join(lines),
        reply_markup=MAIN_KEYBOARD,
    )


async def _send_images(
    message,
    files: list[Path],
    caption: str,
    *,
    parse_mode: str | None = None,
) -> None:
    """Send photos one by one and fall back to documents for invalid dimensions."""

    for index, path in enumerate(files):
        current_caption = caption if index == 0 else None
        reply_markup = MAIN_KEYBOARD if index == len(files) - 1 else None
        try:
            await _send_image_with_retries(
                message,
                path,
                current_caption,
                reply_markup,
                parse_mode,
                as_document=False,
            )
        except BadRequest:
            LOGGER.info("Отправляем %s как документ", path.name, exc_info=True)
            await _send_image_with_retries(
                message,
                path,
                current_caption,
                reply_markup,
                parse_mode,
                as_document=True,
            )
        except OSError:
            LOGGER.exception("Не удалось прочитать локальный файл %s", path)
            await message.reply_text(
                f"Не удалось прочитать файл {path.name}. Проверьте, что он не "
                "перемещён и полностью синхронизирован.",
                reply_markup=MAIN_KEYBOARD,
            )


async def _send_image_with_retries(
    message,
    path: Path,
    caption: str | None,
    reply_markup,
    parse_mode: str | None,
    *,
    as_document: bool,
) -> None:
    """Retry connection failures while reopening the local file each time."""

    for attempt in range(1, SEND_ATTEMPTS + 1):
        try:
            with path.open("rb") as image:
                kwargs = {
                    "caption": caption,
                    "reply_markup": reply_markup,
                    "parse_mode": parse_mode,
                    "connect_timeout": SEND_CONNECT_TIMEOUT,
                    "read_timeout": SEND_READ_TIMEOUT,
                    "write_timeout": SEND_WRITE_TIMEOUT,
                }
                if as_document:
                    await message.reply_document(document=image, **kwargs)
                else:
                    await message.reply_photo(photo=image, **kwargs)
            return
        except NetworkError as exc:
            if attempt == SEND_ATTEMPTS or not _is_connection_failure(exc):
                raise
            delay = 2 ** (attempt - 1)
            LOGGER.warning(
                "Сбой соединения при отправке %s; повтор %s/%s через %s с",
                path.name,
                attempt + 1,
                SEND_ATTEMPTS,
                delay,
            )
            await asyncio.sleep(delay)


def _is_connection_failure(error: BaseException) -> bool:
    """Return true only when retry cannot duplicate an accepted Telegram send."""

    current: BaseException | None = error
    while current is not None:
        if type(current).__name__ in {"ConnectError", "ConnectTimeout"}:
            return True
        current = current.__cause__
    return False


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if error is not None:
        LOGGER.error(
            "Необработанная ошибка при обработке обновления",
            exc_info=(type(error), error, error.__traceback__),
        )
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text(
                "Произошла ошибка. Проверьте интернет и локальные файлы, затем "
                "повторите попытку.",
                reply_markup=MAIN_KEYBOARD,
            )
        except TelegramError:
            LOGGER.warning("Не удалось отправить сообщение об ошибке", exc_info=True)


def _is_start_command(update: Update) -> bool:
    message = update.effective_message
    if message is None or not message.text:
        return False
    parts = message.text.strip().split(maxsplit=1)
    if not parts or not parts[0].startswith("/"):
        return False
    command = parts[0][1:].partition("@")[0].casefold()
    return command == "start"


def _is_active(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    chat = update.effective_chat
    if chat is None:
        return False
    active_chat_ids = context.application.bot_data.get(
        ACTIVE_CHAT_IDS_KEY,
        set(),
    )
    return chat.id in active_chat_ids


def _set_active(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    active: bool,
) -> None:
    chat = update.effective_chat
    if chat is None:
        return
    storage = _storage_or_none(context)
    if storage is not None:
        storage.set_chat_active(chat.id, active)
    active_chat_ids = context.application.bot_data.setdefault(
        ACTIVE_CHAT_IDS_KEY,
        set(),
    )
    if active:
        active_chat_ids.add(chat.id)
    else:
        active_chat_ids.discard(chat.id)


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _catalog(context: ContextTypes.DEFAULT_TYPE) -> MediaCatalog:
    storage = _storage_or_none(context)
    if storage is not None:
        return storage.active_catalog()
    return context.application.bot_data["catalog"]


def _today(settings: Settings):
    local_timezone = timezone(timedelta(hours=settings.utc_offset_hours))
    return datetime.now(local_timezone).date()


def _week_kind(
    target_date,
    settings: Settings,
    storage: BotStorage | None = None,
) -> WeekKind:
    if storage is not None:
        return storage.week_kind(target_date)
    return academic_week_kind(
        target_date,
        settings.academic_year_start_month,
        settings.academic_year_start_day,
    )


def _storage_or_none(
    context: ContextTypes.DEFAULT_TYPE,
) -> BotStorage | None:
    return context.application.bot_data.get("storage")


def run(settings: Settings) -> None:
    """Start long polling and keep the process alive until interrupted."""

    settings.ensure_media_directories()
    application = build_application(settings)
    LOGGER.info("Запуск %s. Для остановки нажмите Ctrl+C.", settings.bot_title)
    # Preserve commands that arrived while polling was being restarted.
    application.run_polling(drop_pending_updates=False)
