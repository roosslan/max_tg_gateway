"""Цикл приёма входящих сообщений MAX (long polling GET /updates).

Обрабатывает два сценария:

1. Ответ-reply в приложении MAX на ранее пересланное из Telegram сообщение.
   По mid исходного сообщения MAX в хранилище находится исходный чат Telegram,
   и текст ответа уходит туда (как reply на исходное сообщение).

2. Сообщение от владельца (его MAX ID) со ссылкой. Ссылка обрабатывается:
   веб-страница возвращается читаемым текстом (длинная — файлом .md), файл —
   пересылается вложением обратно владельцу в MAX.
"""
import sys
import time

import requests

from max_sender import MAX_API, send_file_to_max, send_text_to_max
from tg_sender import send_tg_document, send_tg_message
from web_extract import fetch_url, find_first_url

# Таймаут long polling на стороне MAX (сек).
POLL_TIMEOUT = 30

# Порог длины текста MAX: больше — отдаём файлом .md (лимит сообщения ~4000).
MAX_TEXT_LIMIT = 3500


def _get(obj, *keys):
    """Возвращает первое непустое значение по одному из ключей вложенного dict.

    Args:
        obj: словарь (или не словарь — тогда вернётся None).
        *keys: имена ключей, которые пробуются по порядку.

    Returns:
        Значение первого найденного непустого ключа либо None.
    """
    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value:
            return value
    return None


def _extract_sender_id(message: dict) -> str:
    """Извлекает MAX ID отправителя сообщения (терпимо к формату).

    Args:
        message (dict): объект сообщения MAX.

    Returns:
        str: id отправителя строкой, либо пустая строка.
    """
    sender = _get(message, "sender", "from") or {}
    sid = _get(sender, "user_id", "userId", "id")
    return str(sid).strip() if sid else ""


def _extract_text(message: dict) -> str:
    """Извлекает текст сообщения MAX.

    Args:
        message (dict): объект сообщения MAX.

    Returns:
        str: текст сообщения либо пустая строка.
    """
    body = _get(message, "body") or {}
    text = _get(body, "text") or _get(message, "text")
    return str(text).strip() if text else ""


def _extract_mid(message: dict) -> str:
    """Извлекает mid самого сообщения MAX.

    Args:
        message (dict): объект сообщения MAX.

    Returns:
        str: mid сообщения либо пустая строка.
    """
    body = _get(message, "body") or {}
    mid = _get(body, "mid") or _get(message, "mid")
    return str(mid).strip() if mid else ""


def _extract_reply_mid(message: dict) -> str:
    """Извлекает mid сообщения, на которое данное является ответом (reply).

    MAX кладёт ссылку на исходное сообщение в поле link типа "reply". Точная
    вложенность формата неизвестна, поэтому mid ищется в нескольких местах.

    Args:
        message (dict): объект сообщения MAX.

    Returns:
        str: mid исходного сообщения либо пустая строка, если это не reply.
    """
    link = _get(message, "link") or {}
    if not isinstance(link, dict):
        return ""
    link_type = str(_get(link, "type") or "").lower()
    if link_type and link_type != "reply":
        return ""
    # Пробуем несколько возможных расположений mid внутри link.
    mid = _get(link, "mid")
    if mid:
        return str(mid)
    inner = _get(link, "message") or {}
    inner_body = _get(inner, "body") or {}
    mid = _get(inner_body, "mid") or _get(inner, "mid")
    return str(mid) if mid else ""


def _handle_link_from_owner(max_token: str, owner_target: dict, url: str) -> None:
    """Обрабатывает ссылку от владельца и присылает ответ ему в MAX.

    Args:
        max_token (str): токен бота MAX.
        owner_target (dict): адресат-владелец ({"user_id": ...}).
        url (str): ссылка из сообщения владельца.
    """
    result = fetch_url(url)
    if result.kind == "error":
        send_text_to_max(max_token, owner_target, f"⚠ {result.text}")
        return

    if result.kind == "file":
        send_file_to_max(
            max_token,
            owner_target,
            result.data or b"",
            filename=result.filename,
            content_type=result.content_type,
            caption=result.filename,
        )
        return

    # Читаемый текст.
    header = (result.title + "\n\n") if result.title else ""
    full = header + result.text
    if len(full) <= MAX_TEXT_LIMIT:
        send_text_to_max(max_token, owner_target, full)
    else:
        # Длинный текст — отдаём файлом .md.
        send_file_to_max(
            max_token,
            owner_target,
            full.encode("utf-8"),
            filename="page.md",
            content_type="text/markdown",
            caption=result.title or "Содержимое страницы",
        )


def _handle_reply_to_telegram(tg_token: str, entry: dict, text: str) -> None:
    """Пересылает ответ из MAX обратно в исходный чат Telegram.

    Args:
        tg_token (str): токен Telegram-бота.
        entry (dict): запись хранилища с tg_chat_id и tg_message_id.
        text (str): текст ответа владельца из MAX.
    """
    if not text:
        return
    send_tg_message(
        tg_token,
        entry.get("tg_chat_id"),
        text,
        reply_to_message_id=entry.get("tg_message_id"),
    )


def _handle_message(message: dict, config, store) -> None:
    """Разбирает одно входящее сообщение MAX и выбирает сценарий обработки.

    Сначала проверяет, не является ли сообщение ответом на известное
    пересланное сообщение (reply → маршрутизация в Telegram). Иначе, если
    сообщение от владельца и содержит ссылку, обрабатывает ссылку.

    Args:
        message (dict): объект сообщения MAX.
        config: объект Config (нужны max_token, max_owner_id, tg_token).
        store: хранилище соответствий MappingStore.
    """
    text = _extract_text(message)
    sender_id = _extract_sender_id(message)

    # Сценарий 1: ответ (reply) на пересланное сообщение → в Telegram.
    reply_mid = _extract_reply_mid(message)
    if reply_mid:
        entry = store.lookup(reply_mid)
        if entry:
            _handle_reply_to_telegram(config.tg_token, entry, text)
            return
        # reply на неизвестное сообщение — проваливаемся дальше (вдруг это ссылка).

    # Сценарий 2: ссылка от владельца.
    if config.max_owner_id and sender_id == str(config.max_owner_id):
        url = find_first_url(text)
        if url:
            _handle_link_from_owner(config.max_token, {"user_id": sender_id}, url)


def run_receiver(config, store) -> None:
    """Основной цикл приёма MAX. Блокирующий — запускать в отдельном потоке.

    В бесконечном цикле опрашивает GET /updates (long polling) с маркером,
    разбирает обновления message_created и передаёт сообщения в _handle_message.
    При сетевых ошибках логирует их и ждёт 5 секунд перед повтором.

    Args:
        config: объект Config (нужен max_token; для фич — max_owner_id,
            tg_token).
        store: хранилище соответствий MappingStore.
    """
    max_token = config.max_token
    if not max_token:
        print("[ERROR] Приём MAX включён, но не задан токен [max] token", file=sys.stderr)
        return

    print("[INFO] Цикл приёма MAX запущен.", file=sys.stderr)

    headers = {"Authorization": max_token}
    marker = None
    while True:
        try:
            params = {"timeout": POLL_TIMEOUT, "limit": 100}
            if marker is not None:
                params["marker"] = marker
            resp = requests.get(
                f"{MAX_API}/updates",
                params=params,
                headers=headers,
                timeout=POLL_TIMEOUT + 15,
                verify=False,
            )
            resp.raise_for_status()
            data = resp.json()

            for update in data.get("updates", []):
                # Сырой апдейт в stderr — помогает зафиксировать формат полей MAX.
                print(f"[DEBUG] MAX update: {update}", file=sys.stderr)
                update_type = str(update.get("update_type") or "")
                if update_type and update_type != "message_created":
                    continue
                message = update.get("message")
                if isinstance(message, dict):
                    _handle_message(message, config, store)

            # Сдвигаем marker на следующий ожидаемый апдейт.
            new_marker = data.get("marker")
            if new_marker is not None:
                marker = new_marker
        except Exception as exc:
            print(f"[ERROR] Ошибка опроса MAX: {exc}", file=sys.stderr)
            time.sleep(5)
