"""Telegram handlers and polling entry point."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from typing import Final

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, NetworkError, TelegramError
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
from .settings import Settings, load_bot_token
from .storage import BotStorage


LOGGER = logging.getLogger(__name__)

TODAY_BUTTON: Final = "📅 Сегодня"
MAP_BUTTON: Final = "🗺️ Карта"
HELP_BUTTON: Final = "ℹ️ Помощь"
EXIT_BUTTON: Final = "🚪 Выход"
ACTIVE_CHAT_IDS_KEY: Final = "active_chat_ids"
SEND_ATTEMPTS: Final = 3
SEND_CONNECT_TIMEOUT: Final = 20.0
SEND_READ_TIMEOUT: Final = 30.0
SEND_WRITE_TIMEOUT: Final = 30.0
POLLING_WATCHDOG_INTERVAL_SECONDS: Final = 60.0
POLLING_WATCHDOG_STALL_LIMIT: Final = 3


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
        builder.proxy(settings.proxy_url).get_updates_proxy(settings.proxy_url)
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
    return application


async def initialize_application(application: Application) -> None:
    """Finish startup and start a watchdog for a stalled polling connection."""

    await set_commands(application)
    application.create_task(
        polling_watchdog(application),
        name="polling-watchdog",
    )


async def polling_watchdog(
    application: Application,
    *,
    interval: float = POLLING_WATCHDOG_INTERVAL_SECONDS,
    stall_limit: int = POLLING_WATCHDOG_STALL_LIMIT,
) -> None:
    """Restart the container when Telegram updates remain uncollected."""

    stalled_checks = 0
    while True:
        await asyncio.sleep(interval)
        try:
            webhook = await application.bot.get_webhook_info()
        except TelegramError as exc:
            LOGGER.warning("Не удалось проверить очередь обновлений Telegram: %s", exc)
            continue

        pending = webhook.pending_update_count
        if pending == 0:
            stalled_checks = 0
            continue

        stalled_checks += 1
        LOGGER.warning(
            "Telegram хранит %s необработанных обновлений (%s/%s)",
            pending,
            stalled_checks,
            stall_limit,
        )
        if stalled_checks >= stall_limit:
            LOGGER.error("Polling не забирает обновления; перезапускаю приложение")
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
        "• «Карта» — показывает карту;\n"
        "• «Выход» или /stop — выключает ответы в этом чате.\n\n"
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
    elif text == MAP_BUTTON:
        await map_command(update, context)
    elif text == HELP_BUTTON:
        await help_command(update, context)
    elif text == EXIT_BUTTON:
        await _disable_current_chat(update, context)


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


async def _send_images(message, files: list[Path], caption: str) -> None:
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
                as_document=False,
            )
        except BadRequest:
            LOGGER.info("Отправляем %s как документ", path.name, exc_info=True)
            await _send_image_with_retries(
                message,
                path,
                current_caption,
                reply_markup,
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
