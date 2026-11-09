import json
import unittest
from unittest.mock import AsyncMock

from telegram.error import BadRequest, NetworkError

from schedule_bot.widget_transport import WidgetTransport, encode_state


class WidgetTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_and_edit_use_documented_parameters(self):
        bot = AsyncMock()
        bot.do_api_request.side_effect = [{"message_id": 34}, True]
        transport = WidgetTransport(bot)
        message_id = await transport.send(10011, "<div>Расписание</div>")
        await transport.edit(10011, message_id, encode_state({"patches": []}))

        send, edit = bot.do_api_request.await_args_list
        self.assertEqual(send.args, ("send_widget",))
        self.assertEqual(send.kwargs["api_kwargs"]["chat_id"], 10011)
        self.assertEqual(edit.args, ("edit_message_widget_data",))
        self.assertEqual(edit.kwargs["api_kwargs"]["message_id"], 34)
        self.assertEqual(json.loads(edit.kwargs["api_kwargs"]["public_data"]), {"patches": []})

    async def test_no_production_group_widget_request(self):
        bot = AsyncMock()
        with self.assertRaises(ValueError):
            await WidgetTransport(bot).send(-10011, "<div>test</div>")
        bot.do_api_request.assert_not_awaited()

    async def test_rejection_and_unexpected_result_are_reported(self):
        bot = AsyncMock()
        bot.do_api_request.side_effect = [BadRequest("not supported"), {}]
        transport = WidgetTransport(bot)
        with self.assertRaises(BadRequest):
            await transport.send(1, "<div>test</div>")
        with self.assertRaises(NetworkError):
            await transport.send(1, "<div>test</div>")

    def test_state_limit_in_utf8(self):
        prefix_size = len(encode_state({"text": ""}).encode())
        body = "я" * ((1024 - prefix_size) // 2)
        self.assertLessEqual(len(encode_state({"text": body}).encode()), 1024)
        with self.assertRaises(ValueError):
            encode_state({"text": body + "я"})
