import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from schedule_bot.settings import Settings, ConfigurationError
from tests.test_settings import VALID_CONFIG


class ProxyTests(unittest.TestCase):
    def test_proxy_configuration_and_invalid_urls(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps(VALID_CONFIG))
            for value in ("", "http://172.30.80.1:1081"):
                with self.subTest(value=value), patch.dict(os.environ, {"BOT_PROXY_URL": value}):
                    settings = Settings.load(root)
                    self.assertEqual(settings.proxy_url, value or None)
            for value in ("172.30.80.1:1081", "http://", "http://host:bad", "http://host/path", "socks5://host:1080"):
                with self.subTest(value=value), patch.dict(os.environ, {"BOT_PROXY_URL": value}):
                    with self.assertRaises(ConfigurationError):
                        Settings.load(root)

    def test_both_telegram_clients_use_proxy(self):
        from schedule_bot.bot import build_application
        from telegram.request import HTTPXRequest

        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.json").write_text(json.dumps(VALID_CONFIG))
            with patch.dict(os.environ, {"BOT_PROXY_URL": "http://172.30.80.1:1081"}):
                settings = Settings.load(root)
            proxies = []
            original_init = HTTPXRequest.__init__

            def capture(request, *args, **kwargs):
                proxies.append(kwargs.get("proxy"))
                original_init(request, *args, **kwargs)

            with patch("schedule_bot.bot.load_bot_token", return_value="123456:" + "A" * 30), patch.object(HTTPXRequest, "__init__", capture):
                build_application(settings)
            self.assertEqual(proxies, [settings.proxy_url, settings.proxy_url])
