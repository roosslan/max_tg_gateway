import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import config
import max_sender
import tg_bridge


class LoadConfigTests(unittest.TestCase):
    def test_loads_telegram_bridge_config(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "ini.toml").write_text(
                '[max]\ntoken = "max-token"\n\n'
                '[telegram]\non = "yes"\ntoken = "tg-token"\n\n'
                '[tg_to_max]\n"123" = "-100999"\n"456" = "-100888"\n',
                encoding="utf-8",
            )
            with patch.object(config, "app_dir", return_value=directory):
                cfg = config.load_config()
        self.assertEqual(cfg.tg_enabled, True)
        self.assertEqual(cfg.tg_token, "tg-token")
        # Токен MAX нужен мосту для отправки в чаты MAX.
        self.assertEqual(cfg.max_token, "max-token")
        self.assertEqual(cfg.tg_to_max, {"123": "-100999", "456": "-100888"})

    def test_rejects_missing_configuration_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, "app_dir", return_value=directory):
                with self.assertRaisesRegex(RuntimeError, "Не найден файл конфигурации"):
                    config.load_config()

    def test_rejects_when_bridge_not_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "ini.toml").write_text("# empty\n", encoding="utf-8")
            with patch.object(config, "app_dir", return_value=directory):
                with self.assertRaisesRegex(RuntimeError, "не включён мост"):
                    config.load_config()


class MaxSendTests(unittest.TestCase):
    @patch.object(max_sender.requests, "post")
    def test_forward_text_only_to_max_chat(self, post_mock):
        send_response = MagicMock()
        send_response.json.return_value = {"messageId": 1}
        post_mock.return_value = send_response

        ok = max_sender.forward_to_max_chat("max-token", "-100999", text="привет")

        self.assertTrue(ok)
        # Текст без картинки — ровно один POST на /messages с chat_id.
        self.assertEqual(post_mock.call_count, 1)
        send_call = post_mock.call_args_list[0]
        self.assertTrue(send_call.args[0].endswith("/messages"))
        self.assertEqual(send_call.kwargs["params"], {"chat_id": "-100999"})
        self.assertEqual(send_call.kwargs["json"]["text"], "привет")

    @patch.object(max_sender.threading.Event, "wait", return_value=None)
    @patch.object(max_sender.requests, "post")
    def test_forward_photo_to_max_chat(self, post_mock, _wait_mock):
        upload_info_response = MagicMock()
        upload_info_response.json.return_value = {"url": "https://upload.server/abc"}
        file_upload_response = MagicMock()
        file_upload_response.json.return_value = {"token": "media-token"}
        send_response = MagicMock()
        send_response.json.return_value = {"messageId": 2}
        post_mock.side_effect = [upload_info_response, file_upload_response, send_response]

        ok = max_sender.forward_to_max_chat(
            "max-token", "-100999", text="подпись", png_bytes=b"img", filename="p.jpg"
        )

        self.assertTrue(ok)
        # 2 запроса на загрузку + 1 на отправку.
        self.assertEqual(post_mock.call_count, 3)
        send_call = post_mock.call_args_list[2]
        self.assertTrue(send_call.args[0].endswith("/messages"))
        self.assertEqual(send_call.kwargs["params"], {"chat_id": "-100999"})
        self.assertEqual(len(send_call.kwargs["json"]["attachments"]), 1)


class TgBridgeTests(unittest.TestCase):
    def _make_config(self):
        cfg = config.Config()
        cfg.tg_enabled = True
        cfg.tg_token = "tg-token"
        cfg.max_token = "max-token"
        cfg.tg_to_max = {"123": "-100999"}
        return cfg

    @patch.object(tg_bridge, "forward_to_max_chat")
    def test_forwards_text_from_mapped_sender(self, forward_mock):
        message = {"from": {"id": 123}, "text": "hello", "chat": {"id": -100999}}
        tg_bridge._handle_message(message, {"123": "-100999"}, "tg-token", "max-token")
        forward_mock.assert_called_once()
        _, kwargs = forward_mock.call_args
        self.assertEqual(kwargs["text"], "hello")
        self.assertIsNone(kwargs["png_bytes"])

    @patch.object(tg_bridge, "forward_to_max_chat")
    def test_ignores_unmapped_sender(self, forward_mock):
        message = {"from": {"id": 999}, "text": "hello"}
        tg_bridge._handle_message(message, {"123": "-100999"}, "tg-token", "max-token")
        forward_mock.assert_not_called()

    @patch.object(tg_bridge, "_download_photo", return_value=b"img-bytes")
    @patch.object(tg_bridge, "forward_to_max_chat")
    def test_forwards_photo_with_caption(self, forward_mock, download_mock):
        message = {
            "from": {"id": 123},
            "caption": "подпись",
            "photo": [
                {"file_id": "small"},
                {"file_id": "big"},
            ],
        }
        tg_bridge._handle_message(message, {"123": "-100999"}, "tg-token", "max-token")
        # Берём самый большой размер.
        download_mock.assert_called_once_with("tg-token", "big")
        _, kwargs = forward_mock.call_args
        self.assertEqual(kwargs["text"], "подпись")
        self.assertEqual(kwargs["png_bytes"], b"img-bytes")


if __name__ == "__main__":
    unittest.main()
