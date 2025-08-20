"""Мост Telegram -> MAX.

Слушает сообщения Telegram-бота через long polling (getUpdates).
Когда приходит сообщение от отправителя, чей id указан в таблице
tg_to_max, пересылает его (текст и/или фото) в соответствующий
групповой чат MAX.
"""
import sys
import time

import requests

from max_sender import forward_to_max_chat

TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_FILE = "https://api.telegram.org/file/bot{token}/{file_path}"

# Таймаут long polling на стороне Telegram (сек)
POLL_TIMEOUT = 30


def _tg_get(token: str, method: str, params: dict | None = None, timeout: int = 40):
    """Выполняет GET-запрос к Bot API Telegram и возвращает разобранный JSON.

    Низкоуровневый помощник: подставляет токен и имя метода в шаблон URL,
    передаёт query-параметры и возвращает ответ как dict.

    Args:
        token (str): токен Telegram-бота (от @BotFather).
        method (str): имя метода Bot API (например "getUpdates", "getFile").
        params (dict | None): query-параметры запроса; None → пустой набор.
        timeout (int): таймаут запроса в секундах (по умолчанию 40).

    Returns:
        dict: разобранный JSON-ответ Telegram Bot API.

    Raises:
        requests.HTTPError: при неуспешном HTTP-статусе ответа.
    """
    resp = requests.get(
        TG_API.format(token=token, method=method),
        params=params or {},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _download_photo(token: str, file_id: str) -> bytes | None:
    """Скачивает фото из Telegram по file_id и возвращает его байты.

    Делает два шага: getFile для получения file_path, затем скачивает файл с
    файлового хоста Telegram. При любой ошибке (нет file_path, сетевой сбой)
    логирует её в stderr и возвращает None вместо исключения.

    Args:
        token (str): токен Telegram-бота.
        file_id (str): идентификатор файла из объекта сообщения Telegram.

    Returns:
        bytes | None: бинарное содержимое фото, либо None при неудаче.
    """
    try:
        info = _tg_get(token, "getFile", {"file_id": file_id}, timeout=30)
        file_path = info.get("result", {}).get("file_path")
        if not file_path:
            return None
        resp = requests.get(
            TG_FILE.format(token=token, file_path=file_path),
            timeout=60,
        )
        resp.raise_for_status()
        return resp.content
    except Exception as exc:
        print(f"[ERROR] Failed to download TG photo: {exc}", file=sys.stderr)
        return None


def _handle_message(message: dict, mapping: dict, tg_token: str, max_token: str) -> None:
    """Обрабатывает одно сообщение Telegram и при совпадении пересылает в MAX.

    Определяет ID отправителя; если его нет в таблице соответствия mapping —
    сообщение игнорируется. Иначе берёт текст (или подпись caption) и, если
    есть фото, скачивает самый большой его размер. Если нечего пересылать
    (например, стикер без текста) — выходит. Иначе вызывает
    forward_to_max_chat для отправки в соответствующий групповой чат MAX.

    Args:
        message (dict): объект сообщения Telegram (message или channel_post).
        mapping (dict): таблица соответствия «TG_ID отправителя → chat_id MAX».
        tg_token (str): токен Telegram-бота (нужен для скачивания фото).
        max_token (str): токен бота MAX (нужен для пересылки в чат MAX).

    Returns:
        None.
    """
    sender = message.get("from") or {}
    sender_id = str(sender.get("id", "")).strip()
    if not sender_id:
        return

    chat_id = mapping.get(sender_id)
    if not chat_id:
        # Отправитель не в таблице соответствия — игнорируем.
        return

    text = message.get("text") or message.get("caption") or ""

    png_bytes = None
    filename = "photo.jpg"
    photos = message.get("photo")
    if photos:
        # В photo хранятся размеры от меньшего к большему — берём самый большой.
        file_id = photos[-1].get("file_id")
        if file_id:
            png_bytes = _download_photo(tg_token, file_id)
            filename = f"{file_id}.jpg"

    if not text and not png_bytes:
        # Нечего пересылать (например, стикер или другой тип вложения).
        return

    forward_to_max_chat(max_token, chat_id, text=text, png_bytes=png_bytes, filename=filename)


def run_bridge(config) -> None:
    """Основной цикл моста Telegram → MAX. Блокирующий — запускать в потоке.

    Проверяет наличие токенов Telegram и MAX и непустой таблицы соответствия;
    при их отсутствии логирует ошибку и выходит. Затем в бесконечном цикле
    опрашивает Telegram через long polling (getUpdates с offset), для каждого
    апдейта сдвигает offset и передаёт сообщение (message или channel_post) в
    _handle_message. При сетевых ошибках логирует их и ждёт 5 секунд перед
    повтором.

    Args:
        config: объект Config. Используемые поля:
            - tg_token (str): токен Telegram-бота;
            - max_token (str): токен бота MAX;
            - tg_to_max (dict): таблица «TG_ID отправителя → chat_id MAX».

    Returns:
        None (в норме не возвращается — работает до завершения потока/процесса).
    """
    tg_token = config.tg_token
    max_token = config.max_token
    mapping = config.tg_to_max

    if not tg_token:
        print("[ERROR] Telegram-мост включён, но не задан токен [telegram] token", file=sys.stderr)
        return
    if not max_token:
        print("[ERROR] Telegram-мост включён, но не задан токен MAX [max] token", file=sys.stderr)
        return
    if not mapping:
        print("[ERROR] Telegram-мост включён, но пуста таблица [tg_to_max]", file=sys.stderr)
        return

    print(f"[INFO] Telegram->MAX мост запущен. Отслеживаемые TG_ID: {list(mapping)}", file=sys.stderr)

    offset = None
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset
            data = _tg_get(token=tg_token, method="getUpdates", params=params)

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("channel_post")
                if isinstance(message, dict):
                    _handle_message(message, mapping, tg_token, max_token)
        except Exception as exc:
            print(f"[ERROR] TG poll error: {exc}", file=sys.stderr)
            time.sleep(5)
