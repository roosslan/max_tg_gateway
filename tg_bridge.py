"""Мост Telegram -> MAX.

Слушает сообщения Telegram-бота через long polling (getUpdates). Когда приходит
сообщение от отправителя, чей id указан в таблице tg_to_max, пересылает его
(текст и/или фото) в соответствующий чат MAX. К тексту добавляется подпись
источника («ЛС | Иван», «Группа «…» | Пётр»), а соответствие «mid сообщения
MAX → исходное сообщение Telegram» сохраняется в хранилище — чтобы ответ из
MAX можно было вернуть в нужный чат Telegram.
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
    файлового хоста Telegram. При любой ошибке логирует её в stderr и возвращает
    None вместо исключения.

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
        print(f"[ERROR] Не удалось скачать фото TG: {exc}", file=sys.stderr)
        return None


def _sender_name(message: dict) -> str:
    """Формирует отображаемое имя отправителя сообщения Telegram.

    Args:
        message (dict): объект сообщения Telegram.

    Returns:
        str: имя (имя+фамилия, либо @username, либо id), либо пустая строка.
    """
    sender = message.get("from") or {}
    first = (sender.get("first_name") or "").strip()
    last = (sender.get("last_name") or "").strip()
    name = (first + " " + last).strip()
    if name:
        return name
    if sender.get("username"):
        return "@" + sender["username"]
    return str(sender.get("id", "")).strip()


def _origin_label(message: dict) -> str:
    """Строит подпись источника для пересланного в MAX сообщения.

    Примеры: «ЛС | Иван», «Группа «Чат» | Пётр», «Канал «Новости»».

    Args:
        message (dict): объект сообщения Telegram.

    Returns:
        str: подпись источника.
    """
    chat = message.get("chat") or {}
    chat_type = chat.get("type", "")
    name = _sender_name(message)
    title = (chat.get("title") or "").strip()

    if chat_type == "private":
        return f"ЛС | {name}" if name else "ЛС"
    if chat_type in ("group", "supergroup"):
        base = f"Группа «{title}»" if title else "Группа"
        return f"{base} | {name}" if name else base
    if chat_type == "channel":
        return f"Канал «{title}»" if title else "Канал"
    return name or "Telegram"


def _handle_message(message: dict, mapping: dict, tg_token: str, max_token: str, store) -> None:
    """Обрабатывает одно сообщение Telegram и при совпадении пересылает в MAX.

    Если id отправителя есть в таблице mapping, берёт текст (или подпись),
    при наличии фото скачивает самый большой размер, добавляет подпись
    источника и пересылает в соответствующий чат MAX. Полученный mid сообщения
    MAX сохраняется в хранилище вместе с привязкой к исходному сообщению
    Telegram (для обратной маршрутизации ответов).

    Args:
        message (dict): объект сообщения Telegram (message или channel_post).
        mapping (dict): таблица соответствия «TG_ID отправителя/чата → chat_id MAX».
        tg_token (str): токен Telegram-бота (нужен для скачивания фото).
        max_token (str): токен бота MAX (нужен для пересылки в чат MAX).
        store: хранилище соответствий MappingStore.

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
            if png_bytes is None:
                raise RuntimeError("Не удалось скачать фотографию Telegram")
            filename = f"{file_id}.jpg"

    if not text and not png_bytes:
        # Нечего пересылать (например, стикер или другой тип вложения).
        return

    # Добавляем подпись источника, чтобы при ответе в MAX было видно, кому отвечать.
    label = _origin_label(message)
    forward_text = f"{label}:\n{text}" if text else label

    mid = forward_to_max_chat(max_token, chat_id, text=forward_text, png_bytes=png_bytes, filename=filename)

    if not mid:
        raise RuntimeError("Не удалось доставить сообщение в MAX")

    # Запоминаем соответствие для обратной маршрутизации ответа из MAX.
    if mid:
        tg_chat = message.get("chat") or {}
        store.remember(
            mid,
            tg_chat_id=tg_chat.get("id"),
            tg_message_id=message.get("message_id"),
            label=label,
        )


def run_bridge(config, store) -> None:
    """Основной цикл моста Telegram → MAX. Блокирующий — запускать в потоке.

    Проверяет наличие токенов и непустой таблицы соответствия; при их отсутствии
    логирует ошибку и выходит. Затем в бесконечном цикле опрашивает Telegram
    через long polling (getUpdates с offset) и передаёт сообщения в
    _handle_message. При сетевых ошибках логирует их и ждёт 5 секунд.

    Args:
        config: объект Config (tg_token, max_token, tg_to_max).
        store: хранилище соответствий MappingStore.

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
                message = update.get("message") or update.get("channel_post")
                if isinstance(message, dict):
                    _handle_message(message, mapping, tg_token, max_token, store)
                offset = update["update_id"] + 1
        except Exception as exc:
            print(f"[ERROR] Ошибка опроса TG: {exc}", file=sys.stderr)
            time.sleep(5)
