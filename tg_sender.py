"""Отправка сообщений и файлов в Telegram от имени бота.

Используется циклом приёма MAX: когда владелец отвечает в приложении MAX на
пересланное сообщение, ответ уходит обратно в исходный чат Telegram, при
необходимости как reply на исходное сообщение.
"""
import io
import sys

import requests

TG_API = "https://api.telegram.org/bot{token}/{method}"


def send_tg_message(token: str, chat_id, text: str, reply_to_message_id=None) -> bool:
    """Отправляет текстовое сообщение в чат Telegram.

    Args:
        token (str): токен Telegram-бота.
        chat_id: id чата-адресата (пользователь или группа).
        text (str): текст сообщения.
        reply_to_message_id: message_id сообщения, на которое это является
            ответом; None — обычное сообщение.

    Returns:
        bool: True при успешной отправке, False при ошибке.
    """
    payload: dict = {"chat_id": chat_id, "text": text}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        # Не падать, если исходное сообщение удалено — отправить обычным.
        payload["allow_sending_without_reply"] = True
    try:
        resp = requests.post(
            TG_API.format(token=token, method="sendMessage"),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        print(f"[SUCCESS] Ответ отправлен в Telegram чат {chat_id}", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"[ERROR] Не удалось отправить в Telegram чат {chat_id}: {exc}", file=sys.stderr)
        return False


def send_tg_document(
    token: str,
    chat_id,
    data: bytes,
    filename: str,
    caption: str = "",
    reply_to_message_id=None,
) -> bool:
    """Отправляет файл (документ) в чат Telegram.

    Args:
        token (str): токен Telegram-бота.
        chat_id: id чата-адресата.
        data (bytes): содержимое файла.
        filename (str): имя файла.
        caption (str): подпись к файлу; по умолчанию пустая.
        reply_to_message_id: message_id сообщения-оригинала для reply; None —
            обычная отправка.

    Returns:
        bool: True при успешной отправке, False при ошибке.
    """
    payload: dict = {"chat_id": chat_id}
    if caption:
        payload["caption"] = caption
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
        payload["allow_sending_without_reply"] = True
    try:
        resp = requests.post(
            TG_API.format(token=token, method="sendDocument"),
            data=payload,
            files={"document": (filename, io.BytesIO(data))},
            timeout=60,
        )
        resp.raise_for_status()
        print(f"[SUCCESS] Файл отправлен в Telegram чат {chat_id}", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"[ERROR] Не удалось отправить файл в Telegram чат {chat_id}: {exc}", file=sys.stderr)
        return False
