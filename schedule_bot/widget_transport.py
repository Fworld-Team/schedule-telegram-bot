"""Documented Bot API 10.4 calls through PTB's existing request client."""

import json

from telegram import Bot
from telegram.error import NetworkError

MAX_STATE_BYTES = 1024
CAPTION = "Расписание ДЭ 23-24 и ДЭ 24-24. Если карточка не открывается, выберите день в меню."


def encode_state(state: dict) -> str:
    encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > MAX_STATE_BYTES:
        raise ValueError("Telegram widget state exceeds 1024 bytes")
    return encoded


class WidgetTransport:
    def __init__(self, bot: Bot):
        self.bot = bot

    async def send(self, chat_id: int, html: str, *, thread_id: int | None = None,
                   silent: bool = False) -> int:
        if chat_id <= 0:
            raise ValueError("Telegram production widgets require a private chat")
        params = {"chat_id": chat_id, "html": html, "caption": CAPTION,
                  "disable_notification": silent}
        if thread_id is not None:
            params["message_thread_id"] = thread_id
        result = await self.bot.do_api_request("send_widget", api_kwargs=params, read_timeout=20)
        if not isinstance(result, dict) or type(result.get("message_id")) is not int:
            raise NetworkError("Unexpected sendWidget result")
        return result["message_id"]

    async def edit(self, chat_id: int, message_id: int, state: str) -> None:
        result = await self.bot.do_api_request("edit_message_widget_data", api_kwargs={
            "chat_id": chat_id, "message_id": message_id, "public_data": state,
        }, read_timeout=20)
        if result is not True:
            raise NetworkError("Unexpected editMessageWidgetData result")
