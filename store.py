"""Потокобезопасное хранилище соответствий «сообщение MAX → источник в Telegram».

Когда мост пересылает сообщение из Telegram в чат MAX, он запоминает здесь
mid отправленного сообщения MAX и привязку к исходному сообщению Telegram
(чат и message_id). Позже, когда владелец отвечает в приложении MAX на это
пересланное сообщение, цикл приёма MAX по mid находит, куда в Telegram
направить ответ.

Хранилище персистентное (JSON-файл) и переживает перезапуск процесса.
Размер ограничен: при превышении лимита удаляются самые старые записи.
"""
import json
import os
import sys
import tempfile
import threading
import time
from collections import OrderedDict


class MappingStore:
    """JSON-хранилище соответствий mid MAX → исходное сообщение Telegram.

    Все операции защищены блокировкой, поэтому хранилище безопасно
    использовать из нескольких потоков (цикл Telegram пишет, цикл MAX читает).
    """

    def __init__(self, path: str, max_entries: int = 5000) -> None:
        """Создаёт хранилище и загружает существующие записи с диска.

        Args:
            path (str): путь к JSON-файлу хранилища.
            max_entries (int): максимум записей; при превышении самые старые
                удаляются. По умолчанию 5000.
        """
        self._path = path
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, dict]" = OrderedDict()
        self._load()

    def _load(self) -> None:
        """Читает записи из файла. При отсутствии/повреждении начинает с пустого."""
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get("entries", {})
            if isinstance(entries, dict):
                self._entries = OrderedDict(entries)
        except FileNotFoundError:
            self._entries = OrderedDict()
        except Exception as exc:
            print(f"[ERROR] Не удалось прочитать хранилище {self._path}: {exc}", file=sys.stderr)
            self._entries = OrderedDict()

    def _save_locked(self) -> None:
        """Атомарно записывает текущие записи в файл. Вызывать под блокировкой."""
        directory = os.path.dirname(os.path.abspath(self._path))
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".store-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"version": 1, "entries": self._entries}, f, ensure_ascii=False)
                os.replace(tmp_path, self._path)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        except Exception as exc:
            print(f"[ERROR] Не удалось сохранить хранилище {self._path}: {exc}", file=sys.stderr)

    def remember(self, max_mid: str, tg_chat_id, tg_message_id, label: str = "") -> None:
        """Запоминает, какому сообщению Telegram соответствует сообщение MAX.

        Args:
            max_mid (str): mid сообщения MAX, которое мост отправил в чат MAX.
            tg_chat_id: id исходного чата Telegram (куда уйдёт ответ).
            tg_message_id: message_id исходного сообщения Telegram (для reply).
            label (str): человекочитаемая подпись источника (для логов/отладки).
        """
        if not max_mid:
            return
        with self._lock:
            self._entries[str(max_mid)] = {
                "tg_chat_id": tg_chat_id,
                "tg_message_id": tg_message_id,
                "label": label,
                "ts": int(time.time()),
            }
            self._entries.move_to_end(str(max_mid))
            # Обрезаем самые старые записи при превышении лимита.
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            self._save_locked()

    def lookup(self, max_mid: str) -> dict | None:
        """Возвращает привязку по mid сообщения MAX или None, если её нет.

        Args:
            max_mid (str): mid сообщения MAX, на которое пришёл ответ.

        Returns:
            dict | None: запись с ключами tg_chat_id, tg_message_id, label, ts,
                либо None, если соответствие не найдено.
        """
        if not max_mid:
            return None
        with self._lock:
            entry = self._entries.get(str(max_mid))
            return dict(entry) if entry else None
