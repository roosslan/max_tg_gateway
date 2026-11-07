import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import config
import max_sender
import max_receiver
import tg_bridge
import web_extract
from store import MappingStore
from web_extract import FetchResult


class LoadConfigTests(unittest.TestCase):
    def test_loads_telegram_bridge_config(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "ini.toml").write_text(
                '[max]\ntoken = "max-token"\nowner_id = "405492831"\n\n'
                '[telegram]\non = "yes"\ntoken = "tg-token"\n\n'
                '[tg_to_max]\n"123" = "-100999"\n"456" = "-100888"\n',
                encoding="utf-8",
            )
            with patch.object(config, "app_dir", return_value=directory):
                cfg = config.load_config()
        self.assertEqual(cfg.tg_enabled, True)
        self.assertEqual(cfg.tg_token, "tg-token")
        self.assertEqual(cfg.max_token, "max-token")
        self.assertEqual(cfg.max_owner_id, "405492831")
        self.assertEqual(cfg.tg_to_max, {"123": "-100999", "456": "-100888"})
        self.assertTrue(cfg.state_file.endswith("state.json"))

    def test_loads_max_only_config(self):
        # Только токен MAX, без моста — допустимо (режим приёма MAX).
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "ini.toml").write_text(
                '[max]\ntoken = "max-token"\nowner_id = "405492831"\n',
                encoding="utf-8",
            )
            with patch.object(config, "app_dir", return_value=directory):
                cfg = config.load_config()
        self.assertEqual(cfg.tg_enabled, False)
        self.assertEqual(cfg.max_token, "max-token")
        self.assertEqual(cfg.max_owner_id, "405492831")

    def test_rejects_missing_configuration_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(config, "app_dir", return_value=directory):
                with self.assertRaisesRegex(RuntimeError, "Не найден файл конфигурации"):
                    config.load_config()

    def test_rejects_when_nothing_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "ini.toml").write_text("# empty\n", encoding="utf-8")
            with patch.object(config, "app_dir", return_value=directory):
                with self.assertRaisesRegex(RuntimeError, "не настроен ни один режим"):
                    config.load_config()


class MaxSendTests(unittest.TestCase):
    @patch.object(max_sender.requests, "post")
    def test_forward_text_only_returns_mid(self, post_mock):
        send_response = MagicMock()
        send_response.json.return_value = {"message": {"body": {"mid": "mid-1"}}}
        post_mock.return_value = send_response

        mid = max_sender.forward_to_max_chat("max-token", "-100999", text="привет")

        self.assertEqual(mid, "mid-1")
        self.assertEqual(post_mock.call_count, 1)
        send_call = post_mock.call_args_list[0]
        self.assertTrue(send_call.args[0].endswith("/messages"))
        self.assertEqual(send_call.kwargs["params"], {"chat_id": "-100999"})
        self.assertEqual(send_call.kwargs["json"]["text"], "привет")

    @patch.object(max_sender.threading.Event, "wait", return_value=None)
    @patch.object(max_sender.requests, "post")
    def test_forward_photo_returns_mid(self, post_mock, _wait_mock):
        upload_info_response = MagicMock()
        upload_info_response.json.return_value = {"url": "https://upload.server/abc"}
        file_upload_response = MagicMock()
        file_upload_response.json.return_value = {"token": "media-token"}
        send_response = MagicMock()
        send_response.json.return_value = {"message": {"body": {"mid": "mid-2"}}}
        post_mock.side_effect = [upload_info_response, file_upload_response, send_response]

        mid = max_sender.forward_to_max_chat(
            "max-token", "-100999", text="подпись", png_bytes=b"img", filename="p.jpg"
        )

        self.assertEqual(mid, "mid-2")
        self.assertEqual(post_mock.call_count, 3)
        send_call = post_mock.call_args_list[2]
        self.assertEqual(len(send_call.kwargs["json"]["attachments"]), 1)

    def test_extract_mid_variants(self):
        self.assertEqual(max_sender._extract_mid({"message": {"body": {"mid": "a"}}}), "a")
        self.assertEqual(max_sender._extract_mid({"body": {"mid": "b"}}), "b")
        self.assertEqual(max_sender._extract_mid({"mid": "c"}), "c")
        self.assertIsNone(max_sender._extract_mid({"nothing": 1}))

    @patch.object(max_sender.requests, "post")
    def test_send_text_with_reply_link(self, post_mock):
        send_response = MagicMock()
        send_response.json.return_value = {"message": {"body": {"mid": "mid-r"}}}
        post_mock.return_value = send_response

        mid = max_sender.send_text_to_max(
            "max-token", {"user_id": "405492831"}, "ответ", reply_to_mid="mid-orig"
        )
        self.assertEqual(mid, "mid-r")
        send_call = post_mock.call_args_list[0]
        self.assertEqual(send_call.kwargs["json"]["link"], {"type": "reply", "mid": "mid-orig"})


class TgBridgeTests(unittest.TestCase):
    @patch.object(tg_bridge, "forward_to_max_chat", return_value="mid-10")
    def test_forwards_with_label_and_remembers(self, forward_mock):
        store = MagicMock()
        message = {
            "from": {"id": 123, "first_name": "Иван"},
            "chat": {"id": 555, "type": "private"},
            "message_id": 42,
            "text": "привет",
        }
        tg_bridge._handle_message(message, {"123": "-100999"}, "tg-token", "max-token", store)
        # Пересылка с подписью-источником.
        _, kwargs = forward_mock.call_args
        self.assertIn("ЛС | Иван", kwargs["text"])
        self.assertIn("привет", kwargs["text"])
        # Соответствие запомнено.
        store.remember.assert_called_once()
        args, kwargs = store.remember.call_args
        self.assertEqual(args[0], "mid-10")
        self.assertEqual(kwargs["tg_chat_id"], 555)
        self.assertEqual(kwargs["tg_message_id"], 42)

    @patch.object(tg_bridge, "forward_to_max_chat")
    def test_ignores_unmapped_sender(self, forward_mock):
        store = MagicMock()
        message = {"from": {"id": 999}, "text": "hello", "chat": {"id": 1, "type": "private"}}
        tg_bridge._handle_message(message, {"123": "-100999"}, "tg-token", "max-token", store)
        forward_mock.assert_not_called()
        store.remember.assert_not_called()

    def test_origin_label_group(self):
        message = {
            "from": {"id": 7, "first_name": "Пётр"},
            "chat": {"id": -100, "type": "supergroup", "title": "Чат"},
        }
        self.assertEqual(tg_bridge._origin_label(message), "Группа «Чат» | Пётр")


class StoreTests(unittest.TestCase):
    def test_remember_lookup_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            store = MappingStore(path)
            store.remember("mid-1", tg_chat_id=555, tg_message_id=42, label="ЛС | Иван")
            entry = store.lookup("mid-1")
            self.assertEqual(entry["tg_chat_id"], 555)
            self.assertEqual(entry["tg_message_id"], 42)
            # Новая инстанция читает сохранённое с диска.
            store2 = MappingStore(path)
            self.assertEqual(store2.lookup("mid-1")["tg_message_id"], 42)
            self.assertIsNone(store2.lookup("нет-такого"))

    def test_prune_oldest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.json")
            store = MappingStore(path, max_entries=2)
            store.remember("a", 1, 1)
            store.remember("b", 2, 2)
            store.remember("c", 3, 3)
            self.assertIsNone(store.lookup("a"))  # вытеснен
            self.assertIsNotNone(store.lookup("b"))
            self.assertIsNotNone(store.lookup("c"))


class WebExtractTests(unittest.TestCase):
    def test_find_first_url(self):
        self.assertEqual(
            web_extract.find_first_url("текст https://example.com/a?b=1 ещё"),
            "https://example.com/a?b=1",
        )
        self.assertIsNone(web_extract.find_first_url("без ссылок"))

    def test_filename_from_url(self):
        self.assertEqual(web_extract._filename_from_url("https://x.ru/doc.pdf", ""), "doc.pdf")
        self.assertEqual(
            web_extract._filename_from_url("https://x.ru/path/", "application/pdf"), "file.pdf"
        )


class MaxReceiverTests(unittest.TestCase):
    def _config(self):
        return SimpleNamespace(
            max_token="max-token",
            max_owner_id="405492831",
            tg_token="tg-token",
        )

    def test_extract_helpers(self):
        msg = {
            "sender": {"user_id": "405492831"},
            "body": {"text": "привет", "mid": "M2"},
            "link": {"type": "reply", "mid": "M1"},
        }
        self.assertEqual(max_receiver._extract_sender_id(msg), "405492831")
        self.assertEqual(max_receiver._extract_text(msg), "привет")
        self.assertEqual(max_receiver._extract_mid(msg), "M2")
        self.assertEqual(max_receiver._extract_reply_mid(msg), "M1")

    @patch.object(max_receiver, "send_tg_message")
    def test_reply_routed_to_telegram(self, send_tg_mock):
        store = MappingStore(os.path.join(tempfile.gettempdir(), "nonexistent_test.json"))
        store._entries.clear()
        store.remember("M1", tg_chat_id=555, tg_message_id=42, label="ЛС | Иван")
        message = {
            "sender": {"user_id": "405492831"},
            "body": {"text": "мой ответ", "mid": "M2"},
            "link": {"type": "reply", "mid": "M1"},
        }
        max_receiver._handle_message(message, self._config(), store)
        send_tg_mock.assert_called_once_with(
            "tg-token", 555, "мой ответ", reply_to_message_id=42
        )

    @patch.object(max_receiver, "send_text_to_max")
    @patch.object(max_receiver, "fetch_url")
    def test_link_from_owner_text(self, fetch_mock, send_text_mock):
        fetch_mock.return_value = FetchResult(kind="text", title="Заголовок", text="Тело")
        store = MagicMock()
        store.lookup.return_value = None
        message = {
            "sender": {"user_id": "405492831"},
            "body": {"text": "смотри https://example.com/a"},
        }
        max_receiver._handle_message(message, self._config(), store)
        fetch_mock.assert_called_once_with("https://example.com/a")
        send_text_mock.assert_called_once()
        args, _ = send_text_mock.call_args
        self.assertEqual(args[1], {"user_id": "405492831"})
        self.assertIn("Заголовок", args[2])
        self.assertIn("Тело", args[2])

    @patch.object(max_receiver, "send_text_to_max")
    @patch.object(max_receiver, "fetch_url")
    def test_link_from_non_owner_ignored(self, fetch_mock, send_text_mock):
        store = MagicMock()
        store.lookup.return_value = None
        message = {
            "sender": {"user_id": "999"},
            "body": {"text": "смотри https://example.com/a"},
        }
        max_receiver._handle_message(message, self._config(), store)
        fetch_mock.assert_not_called()
        send_text_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
