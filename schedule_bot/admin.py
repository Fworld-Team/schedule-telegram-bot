"""Private Telegram administrator panel."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
import warnings

from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from .calendar import WEEKDAY_NAMES_RU, WeekKind, week_monday
from .settings import Settings
from .storage import (
    ASSESSMENT_TYPE_LABELS,
    MAX_SUBJECT_NAME_LENGTH,
    BotStorage,
    SemesterAssessment,
    StorageError,
)


LOGGER = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
EXIT_BUTTON_PATTERN = r"^\s*🚪 Выход\s*$"

# This wizard deliberately follows one chat+user across several callback
# messages and a subsequent photo, so per_message=False is required.
warnings.filterwarnings(
    "ignore",
    message=r"If 'per_message=False'.*",
    category=PTBUserWarning,
)

(
    ADMIN_MENU,
    NEW_SEMESTER_NAME,
    NEW_SEMESTER_DATE,
    NEW_SEMESTER_KIND,
    SELECT_UPLOAD_SEMESTER,
    SELECT_UPLOAD_DAY,
    WAIT_SCHEDULE_IMAGE,
    WAIT_MAP_IMAGE,
    SELECT_ASSESSMENT_SEMESTER,
    ASSESSMENT_MENU,
    ASSESSMENT_ADD_NAME,
    ASSESSMENT_ADD_TYPE,
    ASSESSMENT_ADD_COURSEWORK,
    ASSESSMENT_EDIT_SELECT,
    ASSESSMENT_EDIT_MENU,
    ASSESSMENT_EDIT_NAME,
    ASSESSMENT_EDIT_TYPE,
    ASSESSMENT_EDIT_COURSEWORK,
    ASSESSMENT_DELETE_SELECT,
    ASSESSMENT_DELETE_CONFIRM,
) = range(100, 120)

KIND_LABELS = {
    WeekKind.UPPER: "верхняя",
    WeekKind.LOWER: "нижняя",
}
STATUS_LABELS = {
    "active": "активный",
    "draft": "черновик",
    "archived": "архив",
}
ASSESSMENT_DATA_KEYS = {
    "assessment_semester_id",
    "assessment_id",
    "assessment_subject_name",
    "assessment_type",
}


def build_admin_conversation() -> ConversationHandler:
    """Build a sequential, per-user administrator wizard."""

    exit_button_handler = MessageHandler(
        filters.Regex(EXIT_BUTTON_PATTERN),
        stop_admin,
    )
    return ConversationHandler(
        entry_points=[CommandHandler("admin", admin_start)],
        states={
            ADMIN_MENU: [
                exit_button_handler,
                CallbackQueryHandler(
                    admin_menu_action,
                    pattern=(
                        r"^adm:(new|upload|map|week|activate|assessments|"
                        r"status|close)$"
                    ),
                ),
                CallbackQueryHandler(
                    activate_preview,
                    pattern=r"^adm:act:\d+$",
                ),
                CallbackQueryHandler(
                    activate_confirm,
                    pattern=r"^adm:actyes:\d+$",
                ),
                CallbackQueryHandler(
                    set_week_kind,
                    pattern=r"^adm:setweek:(upper|lower)$",
                ),
                CallbackQueryHandler(
                    clear_week_override,
                    pattern=r"^adm:clearweek$",
                ),
                CallbackQueryHandler(
                    show_admin_menu_callback,
                    pattern=r"^adm:menu$",
                ),
            ],
            NEW_SEMESTER_NAME: [
                exit_button_handler,
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_semester_name,
                )
            ],
            NEW_SEMESTER_DATE: [
                exit_button_handler,
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_semester_date,
                )
            ],
            NEW_SEMESTER_KIND: [
                exit_button_handler,
                CallbackQueryHandler(
                    receive_semester_kind,
                    pattern=r"^adm:newkind:(upper|lower)$",
                ),
                CallbackQueryHandler(
                    show_admin_menu_callback,
                    pattern=r"^adm:menu$",
                ),
            ],
            SELECT_UPLOAD_SEMESTER: [
                exit_button_handler,
                CallbackQueryHandler(
                    select_upload_semester,
                    pattern=r"^adm:upsem:\d+$",
                ),
                CallbackQueryHandler(
                    show_admin_menu_callback,
                    pattern=r"^adm:menu$",
                ),
            ],
            SELECT_UPLOAD_DAY: [
                exit_button_handler,
                CallbackQueryHandler(
                    select_upload_day,
                    pattern=r"^adm:day:[0-4]$",
                ),
                CallbackQueryHandler(
                    show_admin_menu_callback,
                    pattern=r"^adm:menu$",
                ),
            ],
            WAIT_SCHEDULE_IMAGE: [
                exit_button_handler,
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE,
                    receive_schedule_image,
                ),
                MessageHandler(~filters.COMMAND, reject_wrong_upload),
            ],
            WAIT_MAP_IMAGE: [
                exit_button_handler,
                MessageHandler(
                    filters.PHOTO | filters.Document.IMAGE,
                    receive_map_image,
                ),
                MessageHandler(~filters.COMMAND, reject_wrong_upload),
            ],
            SELECT_ASSESSMENT_SEMESTER: [
                exit_button_handler,
                CallbackQueryHandler(
                    select_assessment_semester,
                    pattern=r"^adm:asssem:\d+$",
                ),
                CallbackQueryHandler(
                    show_admin_menu_callback,
                    pattern=r"^adm:menu$",
                ),
            ],
            ASSESSMENT_MENU: [
                exit_button_handler,
                CallbackQueryHandler(
                    assessment_menu_action,
                    pattern=r"^adm:ass:(add|edit|delete):\d+$",
                ),
                CallbackQueryHandler(
                    show_assessment_semesters,
                    pattern=r"^adm:ass:semesters$",
                ),
            ],
            ASSESSMENT_ADD_NAME: [
                exit_button_handler,
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_assessment_name,
                ),
            ],
            ASSESSMENT_ADD_TYPE: [
                exit_button_handler,
                CallbackQueryHandler(
                    receive_assessment_type,
                    pattern=(
                        r"^adm:asstype:(credit|differentiated_credit|exam)$"
                    ),
                ),
                CallbackQueryHandler(
                    return_to_assessment_menu,
                    pattern=r"^adm:ass:back$",
                ),
            ],
            ASSESSMENT_ADD_COURSEWORK: [
                exit_button_handler,
                CallbackQueryHandler(
                    receive_assessment_coursework,
                    pattern=r"^adm:asscourse:(yes|no)$",
                ),
                CallbackQueryHandler(
                    return_to_assessment_menu,
                    pattern=r"^adm:ass:back$",
                ),
            ],
            ASSESSMENT_EDIT_SELECT: [
                exit_button_handler,
                CallbackQueryHandler(
                    select_assessment_to_edit,
                    pattern=r"^adm:assedit:\d+:\d+$",
                ),
                CallbackQueryHandler(
                    return_to_assessment_menu,
                    pattern=r"^adm:ass:back$",
                ),
            ],
            ASSESSMENT_EDIT_MENU: [
                exit_button_handler,
                CallbackQueryHandler(
                    assessment_edit_action,
                    pattern=r"^adm:assfield:(name|type|coursework)$",
                ),
                CallbackQueryHandler(
                    return_to_assessment_menu,
                    pattern=r"^adm:ass:back$",
                ),
            ],
            ASSESSMENT_EDIT_NAME: [
                exit_button_handler,
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    receive_assessment_edit_name,
                ),
            ],
            ASSESSMENT_EDIT_TYPE: [
                exit_button_handler,
                CallbackQueryHandler(
                    receive_assessment_edit_type,
                    pattern=(
                        r"^adm:assnewtype:"
                        r"(credit|differentiated_credit|exam)$"
                    ),
                ),
                CallbackQueryHandler(
                    return_to_assessment_menu,
                    pattern=r"^adm:ass:back$",
                ),
            ],
            ASSESSMENT_EDIT_COURSEWORK: [
                exit_button_handler,
                CallbackQueryHandler(
                    receive_assessment_edit_coursework,
                    pattern=r"^adm:assnewcourse:(yes|no)$",
                ),
                CallbackQueryHandler(
                    return_to_assessment_menu,
                    pattern=r"^adm:ass:back$",
                ),
            ],
            ASSESSMENT_DELETE_SELECT: [
                exit_button_handler,
                CallbackQueryHandler(
                    select_assessment_to_delete,
                    pattern=r"^adm:assdelete:\d+:\d+$",
                ),
                CallbackQueryHandler(
                    return_to_assessment_menu,
                    pattern=r"^adm:ass:back$",
                ),
            ],
            ASSESSMENT_DELETE_CONFIRM: [
                exit_button_handler,
                CallbackQueryHandler(
                    confirm_assessment_delete,
                    pattern=r"^adm:assdeleteconfirm:(yes|no)$",
                ),
            ],
        },
        fallbacks=[
            CommandHandler("stop", stop_admin),
            CommandHandler("cancel", cancel_admin),
        ],
        allow_reentry=True,
        per_chat=True,
        per_user=True,
        per_message=False,
    )


async def stop_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    """End an admin wizard and disable only its current chat."""

    context.user_data.clear()
    # Lazy import avoids the bot -> admin -> bot import cycle.
    from .bot import _disable_current_chat

    await _disable_current_chat(update, context)
    return ConversationHandler.END


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the immutable numeric ID used for administrator authorization."""

    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None:
        return
    lines = [f"Ваш Telegram ID: {user.id}"]
    if chat is not None:
        lines.append(f"ID этого чата: {chat.id}")
    await message.reply_text("\n".join(lines))


async def admin_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    settings = _settings(context)
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return ConversationHandler.END
    if not _is_authorized(update, settings):
        return ConversationHandler.END
    context.user_data.clear()
    await _send_admin_menu(update, context)
    return ADMIN_MENU


async def admin_menu_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    query = update.callback_query
    action = query.data.removeprefix("adm:")
    if action == "new":
        await update.effective_message.reply_text(
            "Введите название нового семестра, например «Весна 2027».\n"
            "Отмена: /cancel"
        )
        return NEW_SEMESTER_NAME
    if action == "upload":
        return await _offer_upload_semesters(update, context)
    if action == "map":
        await update.effective_message.reply_text(
            "Пришлите новую карту как фото или как файл PNG/JPEG/WebP "
            f"размером до {MAX_UPLOAD_BYTES // 1024 // 1024} МБ.\n"
            "Отмена: /cancel"
        )
        return WAIT_MAP_IMAGE
    if action == "week":
        await _show_week_controls(update, context)
        return ADMIN_MENU
    if action == "activate":
        await _show_activation_choices(update, context)
        return ADMIN_MENU
    if action == "assessments":
        return await _offer_assessment_semesters(update, context)
    if action == "status":
        await _send_admin_status(update, context)
        return ADMIN_MENU
    if action == "close":
        context.user_data.clear()
        await update.effective_message.reply_text(
            "Админ-панель закрыта. Обычное меню: /start"
        )
        return ConversationHandler.END
    return ADMIN_MENU


async def receive_semester_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_message(update, context):
        return ConversationHandler.END
    name = " ".join((update.effective_message.text or "").split())
    if not name or len(name) > 80:
        await update.effective_message.reply_text(
            "Название должно содержать от 1 до 80 символов. Попробуйте ещё раз."
        )
        return NEW_SEMESTER_NAME
    context.user_data["new_semester_name"] = name
    await update.effective_message.reply_text(
        f"Название: {name}\n"
        "Введите дату любого дня первой учебной недели в формате "
        "ДД.ММ.ГГГГ, например 08.02.2027.\n"
        "Можно написать «сегодня»."
    )
    return NEW_SEMESTER_DATE


async def receive_semester_date(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_message(update, context):
        return ConversationHandler.END
    raw = (update.effective_message.text or "").strip().casefold()
    if raw in {"сегодня", "today"}:
        selected = _today(context)
    else:
        selected = None
        for format_string in ("%d.%m.%Y", "%Y-%m-%d"):
            try:
                selected = datetime.strptime(raw, format_string).date()
                break
            except ValueError:
                continue
        if selected is None:
            await update.effective_message.reply_text(
                "Не понял дату. Используйте ДД.ММ.ГГГГ, например 08.02.2027."
            )
            return NEW_SEMESTER_DATE
    monday = week_monday(selected)
    context.user_data["new_semester_date"] = selected.isoformat()
    await update.effective_message.reply_text(
        f"Первая неделя: {monday:%d.%m.%Y}–"
        f"{monday + timedelta(days=6):%d.%m.%Y}.\n"
        "Какая она?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬆️ Верхняя",
                        callback_data="adm:newkind:upper",
                    ),
                    InlineKeyboardButton(
                        "⬇️ Нижняя",
                        callback_data="adm:newkind:lower",
                    ),
                ],
                [InlineKeyboardButton("Отмена", callback_data="adm:menu")],
            ]
        ),
    )
    return NEW_SEMESTER_KIND


async def receive_semester_kind(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    query = update.callback_query
    kind = WeekKind(query.data.rsplit(":", 1)[1])
    name = context.user_data.pop("new_semester_name", None)
    raw_date = context.user_data.pop("new_semester_date", None)
    if not name or not raw_date:
        await update.effective_message.reply_text(
            "Данные мастера устарели. Начните создание заново."
        )
        await _send_admin_menu(update, context)
        return ADMIN_MENU
    try:
        semester = _storage(context).create_semester(
            name,
            date.fromisoformat(raw_date),
            kind,
            update.effective_user.id,
        )
    except StorageError as exc:
        await update.effective_message.reply_text(f"Не удалось создать: {exc}")
    else:
        await update.effective_message.reply_text(
            f"Создан черновик «{semester.name}» (ID {semester.id}).\n"
            "Теперь загрузите расписание, затем активируйте семестр. "
            "Пользователи пока продолжают видеть старый."
        )
    await _send_admin_menu(update, context)
    return ADMIN_MENU


async def _offer_assessment_semesters(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    _clear_assessment_data(context)
    semesters = _storage(context).list_semesters()
    buttons = [
        [
            InlineKeyboardButton(
                f"{semester.name} · {STATUS_LABELS[semester.status]}",
                callback_data=f"adm:asssem:{semester.id}",
            )
        ]
        for semester in semesters
    ]
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="adm:menu")])
    await update.effective_message.reply_text(
        "Выберите семестр для управления экзаменами и зачётами:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return SELECT_ASSESSMENT_SEMESTER


async def show_assessment_semesters(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    return await _offer_assessment_semesters(update, context)


async def select_assessment_semester(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    semester_id = int(update.callback_query.data.rsplit(":", 1)[1])
    return await _send_assessment_menu(update, context, semester_id)


async def assessment_menu_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    _, _, action, raw_semester_id = update.callback_query.data.split(":")
    semester_id = int(raw_semester_id)
    semester = _storage(context).get_semester(semester_id)
    if semester is None:
        await update.effective_message.reply_text("Семестр уже не существует.")
        return await _offer_assessment_semesters(update, context)

    _clear_assessment_data(context)
    context.user_data["assessment_semester_id"] = semester_id
    if action == "add":
        await update.effective_message.reply_text("Введите название дисциплины.")
        return ASSESSMENT_ADD_NAME

    assessments = _storage(context).list_semester_assessments(semester_id)
    if not assessments:
        await update.effective_message.reply_text(
            "В этом семестре пока нет дисциплин."
        )
        _clear_assessment_data(context)
        return await _send_assessment_menu(update, context, semester_id)

    if action == "edit":
        prefix = "adm:assedit"
        prompt = "Выберите дисциплину для изменения:"
        next_state = ASSESSMENT_EDIT_SELECT
    else:
        prefix = "adm:assdelete"
        prompt = "Выберите дисциплину для удаления:"
        next_state = ASSESSMENT_DELETE_SELECT
    buttons = [
        [
            InlineKeyboardButton(
                _short_assessment_label(assessment),
                callback_data=(
                    f"{prefix}:{semester_id}:{assessment.id}"
                ),
            )
        ]
        for assessment in assessments
    ]
    buttons.append(
        [InlineKeyboardButton("⬅️ Назад", callback_data="adm:ass:back")]
    )
    await update.effective_message.reply_text(
        prompt,
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return next_state


async def receive_assessment_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_message(update, context):
        return ConversationHandler.END
    name = _clean_subject_name(update.effective_message.text or "")
    if name is None:
        await update.effective_message.reply_text(
            "Название дисциплины должно содержать от 1 до "
            f"{MAX_SUBJECT_NAME_LENGTH} символов. Попробуйте ещё раз."
        )
        return ASSESSMENT_ADD_NAME
    context.user_data["assessment_subject_name"] = name
    await update.effective_message.reply_text(
        "Выберите вид аттестации:",
        reply_markup=_assessment_type_keyboard("adm:asstype"),
    )
    return ASSESSMENT_ADD_TYPE


async def receive_assessment_type(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    assessment_type = update.callback_query.data.rsplit(":", 1)[1]
    context.user_data["assessment_type"] = assessment_type
    await update.effective_message.reply_text(
        "Есть курсовая работа?",
        reply_markup=_coursework_keyboard("adm:asscourse"),
    )
    return ASSESSMENT_ADD_COURSEWORK


async def receive_assessment_coursework(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    semester_id = context.user_data.get("assessment_semester_id")
    subject_name = context.user_data.get("assessment_subject_name")
    assessment_type = context.user_data.get("assessment_type")
    has_coursework = update.callback_query.data.endswith(":yes")
    if (
        not isinstance(semester_id, int)
        or not isinstance(subject_name, str)
        or assessment_type not in ASSESSMENT_TYPE_LABELS
    ):
        _clear_assessment_data(context)
        await update.effective_message.reply_text(
            "Данные мастера устарели. Начните добавление заново."
        )
        return await _offer_assessment_semesters(update, context)
    try:
        assessment = _storage(context).create_semester_assessment(
            semester_id,
            subject_name,
            assessment_type,
            has_coursework,
            update.effective_user.id,
        )
    except StorageError as exc:
        await update.effective_message.reply_text(f"Не удалось добавить: {exc}")
    else:
        await update.effective_message.reply_text(
            f"Добавлено: {_assessment_line(assessment)}"
        )
    _clear_assessment_data(context)
    return await _send_assessment_menu(update, context, semester_id)


async def select_assessment_to_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    _, _, raw_semester_id, raw_assessment_id = (
        update.callback_query.data.split(":")
    )
    semester_id = int(raw_semester_id)
    assessment_id = int(raw_assessment_id)
    assessment = _storage(context).get_semester_assessment(
        semester_id,
        assessment_id,
    )
    if assessment is None:
        await update.effective_message.reply_text("Дисциплина уже не существует.")
        _clear_assessment_data(context)
        return await _send_assessment_menu(update, context, semester_id)
    context.user_data["assessment_semester_id"] = semester_id
    context.user_data["assessment_id"] = assessment_id
    return await _send_assessment_edit_menu(update, assessment)


async def assessment_edit_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    assessment = _selected_assessment(context)
    if assessment is None:
        await update.effective_message.reply_text(
            "Данные редактирования устарели. Выберите дисциплину заново."
        )
        return await _return_to_selected_assessment_menu(update, context)
    field = update.callback_query.data.rsplit(":", 1)[1]
    if field == "name":
        await update.effective_message.reply_text(
            "Введите новое название дисциплины."
        )
        return ASSESSMENT_EDIT_NAME
    if field == "type":
        await update.effective_message.reply_text(
            "Выберите новый вид аттестации:",
            reply_markup=_assessment_type_keyboard("adm:assnewtype"),
        )
        return ASSESSMENT_EDIT_TYPE
    await update.effective_message.reply_text(
        "Есть курсовая работа?",
        reply_markup=_coursework_keyboard("adm:assnewcourse"),
    )
    return ASSESSMENT_EDIT_COURSEWORK


async def receive_assessment_edit_name(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_message(update, context):
        return ConversationHandler.END
    name = _clean_subject_name(update.effective_message.text or "")
    if name is None:
        await update.effective_message.reply_text(
            "Название дисциплины должно содержать от 1 до "
            f"{MAX_SUBJECT_NAME_LENGTH} символов. Попробуйте ещё раз."
        )
        return ASSESSMENT_EDIT_NAME
    return await _apply_assessment_update(
        update,
        context,
        subject_name=name,
        failure_state=ASSESSMENT_EDIT_NAME,
    )


async def receive_assessment_edit_type(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    assessment_type = update.callback_query.data.rsplit(":", 1)[1]
    return await _apply_assessment_update(
        update,
        context,
        assessment_type=assessment_type,
        failure_state=ASSESSMENT_EDIT_TYPE,
    )


async def receive_assessment_edit_coursework(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    return await _apply_assessment_update(
        update,
        context,
        has_coursework=update.callback_query.data.endswith(":yes"),
        failure_state=ASSESSMENT_EDIT_COURSEWORK,
    )


async def select_assessment_to_delete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    _, _, raw_semester_id, raw_assessment_id = (
        update.callback_query.data.split(":")
    )
    semester_id = int(raw_semester_id)
    assessment_id = int(raw_assessment_id)
    assessment = _storage(context).get_semester_assessment(
        semester_id,
        assessment_id,
    )
    if assessment is None:
        await update.effective_message.reply_text("Дисциплина уже не существует.")
        _clear_assessment_data(context)
        return await _send_assessment_menu(update, context, semester_id)
    context.user_data["assessment_semester_id"] = semester_id
    context.user_data["assessment_id"] = assessment_id
    await update.effective_message.reply_text(
        f"Удалить «{_assessment_line(assessment)}»?",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Да, удалить",
                        callback_data="adm:assdeleteconfirm:yes",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Нет",
                        callback_data="adm:assdeleteconfirm:no",
                    )
                ],
            ]
        ),
    )
    return ASSESSMENT_DELETE_CONFIRM


async def confirm_assessment_delete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    semester_id = context.user_data.get("assessment_semester_id")
    assessment_id = context.user_data.get("assessment_id")
    if not isinstance(semester_id, int) or not isinstance(assessment_id, int):
        _clear_assessment_data(context)
        await update.effective_message.reply_text(
            "Данные удаления устарели. Выберите дисциплину заново."
        )
        return await _offer_assessment_semesters(update, context)
    if update.callback_query.data.endswith(":yes"):
        removed = _storage(context).delete_semester_assessment(
            semester_id,
            assessment_id,
            update.effective_user.id,
        )
        await update.effective_message.reply_text(
            "Дисциплина удалена."
            if removed
            else "Дисциплина уже была удалена."
        )
    else:
        await update.effective_message.reply_text("Удаление отменено.")
    _clear_assessment_data(context)
    return await _send_assessment_menu(update, context, semester_id)


async def return_to_assessment_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    return await _return_to_selected_assessment_menu(update, context)


async def select_upload_semester(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    semester_id = int(update.callback_query.data.rsplit(":", 1)[1])
    semester = _storage(context).get_semester(semester_id)
    if semester is None:
        await update.effective_message.reply_text("Семестр уже не существует.")
        return await _offer_upload_semesters(update, context)
    context.user_data["upload_semester_id"] = semester_id
    await update.effective_message.reply_text(
        f"Семестр: {semester.name}\nВыберите день:",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        WEEKDAY_NAMES_RU[index],
                        callback_data=f"adm:day:{index}",
                    )
                ]
                for index in range(5)
            ]
            + [[InlineKeyboardButton("Отмена", callback_data="adm:menu")]]
        ),
    )
    return SELECT_UPLOAD_DAY


async def select_upload_day(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    weekday = int(update.callback_query.data.rsplit(":", 1)[1])
    context.user_data["upload_weekday"] = weekday
    context.user_data["upload_variant"] = "common"
    await update.effective_message.reply_text(
        f"{WEEKDAY_NAMES_RU[weekday]}: пришлите общую фотографию расписания "
        "с верхней и нижней неделей как фото или файл PNG/JPEG/WebP "
        f"размером до {MAX_UPLOAD_BYTES // 1024 // 1024} МБ.\n"
        "Старое изображение этого дня сохранится в архиве.\n"
        "Отмена: /cancel"
    )
    return WAIT_SCHEDULE_IMAGE


async def receive_schedule_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_message(update, context):
        return ConversationHandler.END
    semester_id = context.user_data.get("upload_semester_id")
    weekday = context.user_data.get("upload_weekday")
    if not isinstance(semester_id, int) or not isinstance(weekday, int):
        await update.effective_message.reply_text(
            "Данные мастера потеряны. Начните загрузку заново."
        )
        await _send_admin_menu(update, context)
        return ADMIN_MENU
    downloaded = await _download_image(update, context)
    if downloaded is None:
        return WAIT_SCHEDULE_IMAGE
    try:
        target = _storage(context).install_schedule_image(
            downloaded,
            semester_id,
            weekday,
            "common",
            update.effective_user.id,
        )
    except (OSError, StorageError) as exc:
        await update.effective_message.reply_text(
            f"Файл не принят: {exc}\nПришлите другое изображение или /cancel."
        )
        return WAIT_SCHEDULE_IMAGE
    finally:
        downloaded.unlink(missing_ok=True)
    context.user_data.clear()
    await update.effective_message.reply_text(
        f"Готово: сохранён файл {target.name}."
    )
    await _send_admin_menu(update, context)
    return ADMIN_MENU


async def receive_map_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_message(update, context):
        return ConversationHandler.END
    downloaded = await _download_image(update, context)
    if downloaded is None:
        return WAIT_MAP_IMAGE
    try:
        target = _storage(context).install_map_image(
            downloaded,
            update.effective_user.id,
        )
    except (OSError, StorageError) as exc:
        await update.effective_message.reply_text(
            f"Файл не принят: {exc}\nПришлите другое изображение или /cancel."
        )
        return WAIT_MAP_IMAGE
    finally:
        downloaded.unlink(missing_ok=True)
    await update.effective_message.reply_text(f"Карта обновлена: {target.name}.")
    await _send_admin_menu(update, context)
    return ADMIN_MENU


async def reject_wrong_upload(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_message(update, context):
        return ConversationHandler.END
    await update.effective_message.reply_text(
        "Нужно прислать изображение как фото либо файл PNG/JPEG/WebP. "
        "Для отмены: /cancel"
    )
    return None


async def set_week_kind(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    kind = WeekKind(update.callback_query.data.rsplit(":", 1)[1])
    _storage(context).set_current_week_kind(
        _today(context),
        kind,
        update.effective_user.id,
    )
    next_kind = (
        WeekKind.LOWER if kind is WeekKind.UPPER else WeekKind.UPPER
    )
    await update.effective_message.reply_text(
        f"Исправлено: текущая неделя — {KIND_LABELS[kind]}, "
        f"следующая — {KIND_LABELS[next_kind]}. "
        "Дальше чередование продолжится автоматически."
    )
    await _send_admin_menu(update, context)
    return ADMIN_MENU


async def clear_week_override(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    removed = _storage(context).clear_current_week_override(
        _today(context),
        update.effective_user.id,
    )
    await update.effective_message.reply_text(
        "Коррекция текущей недели удалена."
        if removed
        else "Для текущей недели отдельной коррекции не было."
    )
    await _send_admin_menu(update, context)
    return ADMIN_MENU


async def activate_preview(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    semester_id = int(update.callback_query.data.rsplit(":", 1)[1])
    semester = _storage(context).get_semester(semester_id)
    if semester is None:
        await update.effective_message.reply_text("Семестр не найден.")
        return ADMIN_MENU
    coverage = _storage(context).coverage(semester_id)
    ready = _covered_days(coverage)
    warning = (
        "\n⚠️ Расписание неполное; отсутствующие дни пользователи не увидят."
        if ready < 5
        else "\nВсе пять учебных дней заполнены."
    )
    await update.effective_message.reply_text(
        f"Активировать «{semester.name}»?\n"
        f"Заполнено дней: {ready}/5.{warning}\n"
        "Текущее расписание перейдёт в архив.",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Да, активировать",
                        callback_data=f"adm:actyes:{semester_id}",
                    )
                ],
                [InlineKeyboardButton("Нет", callback_data="adm:menu")],
            ]
        ),
    )
    return ADMIN_MENU


async def activate_confirm(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    semester_id = int(update.callback_query.data.rsplit(":", 1)[1])
    try:
        semester = _storage(context).activate_semester(
            semester_id,
            update.effective_user.id,
        )
    except StorageError as exc:
        await update.effective_message.reply_text(f"Не удалось активировать: {exc}")
    else:
        await update.effective_message.reply_text(
            f"Активирован семестр «{semester.name}». "
            "Пользователи уже получают его расписание."
        )
    await _send_admin_menu(update, context)
    return ADMIN_MENU


async def show_admin_menu_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_callback(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    await _send_admin_menu(update, context)
    return ADMIN_MENU


async def cancel_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    if not await _guard_message(update, context):
        return ConversationHandler.END
    context.user_data.clear()
    await update.effective_message.reply_text(
        "Операция отменена. Админ-панель закрыта; открыть снова: /admin"
    )
    return ConversationHandler.END


async def _send_assessment_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    semester_id: int,
) -> int:
    _clear_assessment_data(context)
    semester = _storage(context).get_semester(semester_id)
    if semester is None:
        await update.effective_message.reply_text("Семестр уже не существует.")
        return await _offer_assessment_semesters(update, context)
    assessments = _storage(context).list_semester_assessments(semester_id)
    lines = [
        "🎓 Экзамены и зачёты",
        f"Семестр: {semester.name} · {STATUS_LABELS[semester.status]}",
        "",
    ]
    if assessments:
        lines.extend(_assessment_line(item) for item in assessments)
    else:
        lines.append("Дисциплины пока не добавлены.")
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Добавить дисциплину",
                        callback_data=f"adm:ass:add:{semester_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✏️ Изменить",
                        callback_data=f"adm:ass:edit:{semester_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🗑 Удалить",
                        callback_data=f"adm:ass:delete:{semester_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Назад",
                        callback_data="adm:ass:semesters",
                    )
                ],
            ]
        ),
    )
    return ASSESSMENT_MENU


async def _send_assessment_edit_menu(
    update: Update,
    assessment: SemesterAssessment,
) -> int:
    await update.effective_message.reply_text(
        f"Изменение дисциплины:\n{_assessment_line(assessment)}",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Название",
                        callback_data="adm:assfield:name",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Тип аттестации",
                        callback_data="adm:assfield:type",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Курсовая",
                        callback_data="adm:assfield:coursework",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Назад",
                        callback_data="adm:ass:back",
                    )
                ],
            ]
        ),
    )
    return ASSESSMENT_EDIT_MENU


async def _apply_assessment_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    subject_name: str | None = None,
    assessment_type: str | None = None,
    has_coursework: bool | None = None,
    failure_state: int,
) -> int:
    current = _selected_assessment(context)
    if current is None:
        await update.effective_message.reply_text(
            "Данные редактирования устарели. Выберите дисциплину заново."
        )
        return await _return_to_selected_assessment_menu(update, context)
    try:
        updated = _storage(context).update_semester_assessment(
            current.semester_id,
            current.id,
            subject_name if subject_name is not None else current.subject_name,
            assessment_type
            if assessment_type is not None
            else current.assessment_type,
            has_coursework
            if has_coursework is not None
            else current.has_coursework,
            update.effective_user.id,
        )
    except StorageError as exc:
        await update.effective_message.reply_text(f"Не удалось изменить: {exc}")
        return failure_state
    semester_id = current.semester_id
    _clear_assessment_data(context)
    await update.effective_message.reply_text(
        f"Изменено: {_assessment_line(updated)}"
    )
    return await _send_assessment_menu(update, context, semester_id)


async def _return_to_selected_assessment_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    semester_id = context.user_data.get("assessment_semester_id")
    _clear_assessment_data(context)
    if not isinstance(semester_id, int):
        return await _offer_assessment_semesters(update, context)
    return await _send_assessment_menu(update, context, semester_id)


def _selected_assessment(
    context: ContextTypes.DEFAULT_TYPE,
) -> SemesterAssessment | None:
    semester_id = context.user_data.get("assessment_semester_id")
    assessment_id = context.user_data.get("assessment_id")
    if not isinstance(semester_id, int) or not isinstance(assessment_id, int):
        return None
    return _storage(context).get_semester_assessment(semester_id, assessment_id)


def _assessment_type_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Зачёт", callback_data=f"{prefix}:credit")],
            [
                InlineKeyboardButton(
                    "Диф. зачёт",
                    callback_data=f"{prefix}:differentiated_credit",
                )
            ],
            [InlineKeyboardButton("Экзамен", callback_data=f"{prefix}:exam")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:ass:back")],
        ]
    )


def _coursework_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Да", callback_data=f"{prefix}:yes"),
                InlineKeyboardButton("Нет", callback_data=f"{prefix}:no"),
            ],
            [InlineKeyboardButton("⬅️ Назад", callback_data="adm:ass:back")],
        ]
    )


def _assessment_line(assessment: SemesterAssessment) -> str:
    coursework = " + курсовая" if assessment.has_coursework else ""
    return (
        f"{assessment.subject_name} — "
        f"{ASSESSMENT_TYPE_LABELS[assessment.assessment_type]}{coursework}"
    )


def _short_assessment_label(assessment: SemesterAssessment) -> str:
    label = _assessment_line(assessment)
    return label if len(label) <= 60 else label[:57].rstrip() + "…"


def _clean_subject_name(raw_name: str) -> str | None:
    cleaned = " ".join(raw_name.split())
    if not cleaned or len(cleaned) > MAX_SUBJECT_NAME_LENGTH:
        return None
    return cleaned


def _clear_assessment_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ASSESSMENT_DATA_KEYS:
        context.user_data.pop(key, None)


async def _offer_upload_semesters(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    semesters = _storage(context).list_semesters()
    buttons = [
        [
            InlineKeyboardButton(
                f"{semester.name} · {STATUS_LABELS[semester.status]}",
                callback_data=f"adm:upsem:{semester.id}",
            )
        ]
        for semester in semesters
    ]
    buttons.append([InlineKeyboardButton("Отмена", callback_data="adm:menu")])
    await update.effective_message.reply_text(
        "В какой семестр загрузить расписание?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return SELECT_UPLOAD_SEMESTER


async def _show_activation_choices(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    choices = [
        semester
        for semester in _storage(context).list_semesters()
        if semester.status != "active"
    ]
    if not choices:
        await update.effective_message.reply_text(
            "Других семестров пока нет. Сначала создайте черновик."
        )
        return
    await update.effective_message.reply_text(
        "Выберите семестр. Можно также вернуть прежний из архива:",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"{semester.name} · {STATUS_LABELS[semester.status]}",
                        callback_data=f"adm:act:{semester.id}",
                    )
                ]
                for semester in choices
            ]
            + [[InlineKeyboardButton("Назад", callback_data="adm:menu")]]
        ),
    )


async def _show_week_controls(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    storage = _storage(context)
    today = _today(context)
    monday = week_monday(today)
    current = storage.week_kind(today)
    following = storage.week_kind(today + timedelta(days=7))
    anchor, anchor_kind, overridden = storage.week_source(today)
    source = "ручная коррекция" if overridden else "настройка семестра"
    await update.effective_message.reply_text(
        f"Сейчас: {KIND_LABELS[current]}\n"
        f"Следующая: {KIND_LABELS[following]}\n"
        f"Опора: {anchor:%d.%m.%Y}, {KIND_LABELS[anchor_kind]} ({source})\n\n"
        f"Изменить неделю {monday:%d.%m}–{monday + timedelta(days=6):%d.%m}:",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Сделать верхней",
                        callback_data="adm:setweek:upper",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Сделать нижней",
                        callback_data="adm:setweek:lower",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "Убрать коррекцию",
                        callback_data="adm:clearweek",
                    )
                ],
                [InlineKeyboardButton("Назад", callback_data="adm:menu")],
            ]
        ),
    )


async def _send_admin_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    storage = _storage(context)
    active = storage.active_semester()
    today = _today(context)
    kind = storage.week_kind(today)
    coverage = storage.coverage(active.id)
    ready = _covered_days(coverage)
    health = "OK" if storage.quick_check() else "ОШИБКА"
    lines = [
        "Состояние бота:",
        f"База данных: {health}",
        f"Активный семестр: {active.name} (ID {active.id})",
        f"Текущая неделя: {KIND_LABELS[kind]}",
        f"Заполнено дней: {ready}/5",
        f"Карт: {len(storage.active_catalog().maps())}",
        f"Каталог данных: {storage.data_directory}",
    ]
    await update.effective_message.reply_text("\n".join(lines))
    await _send_admin_menu(update, context)


async def _send_admin_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    await update.effective_message.reply_text(
        "Админ-панель",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "➕ Новый семестр",
                        callback_data="adm:new",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📅 Загрузить фото дня",
                        callback_data="adm:upload",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🗺 Загрузить карту",
                        callback_data="adm:map",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "↕️ Текущая неделя",
                        callback_data="adm:week",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "✅ Активировать семестр",
                        callback_data="adm:activate",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🎓 Экзамены и зачёты",
                        callback_data="adm:assessments",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔎 Проверка",
                        callback_data="adm:status",
                    )
                ],
                [InlineKeyboardButton("Закрыть", callback_data="adm:close")],
            ]
        ),
    )


async def _download_image(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    media = message.photo[-1] if message.photo else message.document
    if media is None:
        await message.reply_text("Изображение не найдено в сообщении.")
        return None
    if media.file_size is not None and media.file_size > MAX_UPLOAD_BYTES:
        await message.reply_text("Файл слишком большой. Максимум 10 МБ.")
        return None
    temporary = _storage(context).new_upload_path()
    completed = False
    try:
        telegram_file = await media.get_file()
        if (
            telegram_file.file_size is not None
            and telegram_file.file_size > MAX_UPLOAD_BYTES
        ):
            await message.reply_text("Файл слишком большой. Максимум 10 МБ.")
            return None
        await telegram_file.download_to_drive(temporary)
        if temporary.stat().st_size > MAX_UPLOAD_BYTES:
            await message.reply_text("Файл слишком большой. Максимум 10 МБ.")
            return None
        completed = True
    except (OSError, TelegramError):
        LOGGER.warning("Не удалось скачать администраторский файл", exc_info=True)
        await message.reply_text(
            "Не удалось скачать файл. Проверьте соединение и попробуйте ещё раз."
        )
        return None
    finally:
        if not completed:
            temporary.unlink(missing_ok=True)
    return temporary


async def _guard_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    query = update.callback_query
    allowed = _is_authorized(update, _settings(context))
    if query is not None:
        await query.answer()
    return allowed


async def _guard_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> bool:
    return _is_authorized(update, _settings(context))


def _is_authorized(update: Update, settings: Settings) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    return (
        user is not None
        and user.id in settings.admin_user_ids
        and chat is not None
        and chat.type == Chat.PRIVATE
    )


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _storage(context: ContextTypes.DEFAULT_TYPE) -> BotStorage:
    return context.application.bot_data["storage"]


def _today(context: ContextTypes.DEFAULT_TYPE):
    # Imported lazily to keep the same timezone helper as public handlers.
    from .bot import _today as public_today

    return public_today(_settings(context))


def _covered_days(coverage: dict[tuple[int, WeekKind], int]) -> int:
    """Count days that can be shown regardless of the current week kind."""

    return sum(
        all(coverage.get((weekday, kind), 0) > 0 for kind in WeekKind)
        for weekday in range(5)
    )
