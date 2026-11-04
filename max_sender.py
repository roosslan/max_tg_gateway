"""Отправка и загрузка сообщений в мессенджер MAX (platform-api2.max.ru).

Содержит низкоуровневые помощники загрузки вложений и отправки сообщений, а
также высокоуровневые функции: пересылка из Telegram в чат MAX (с возвратом
mid), отправка текста и отправка файла (с поддержкой ответа-reply по mid).
"""
import io
import sys
import threading

import requests


MAX_API = "https://platform-api2.max.ru"

# Пауза после загрузки вложения, чтобы сервер успел его обработать (сек).
UPLOAD_PROCESS_DELAY = 3


def _upload_to_max(token: str, data: bytes, filename: str, upload_type: str, content_type: str) -> str:
    """Загружает вложение в MAX и возвращает media-токен.

    Делает два запроса: получение URL для загрузки (POST /uploads?type=<...>)
    и саму загрузку файла на полученный URL. Токен извлекается терпимо: сначала
    поле `token`, затем, для изображений, `photos[photoId]['token']`.

    Args:
        token (str): токен авторизации бота MAX (заголовок Authorization).
        data (bytes): бинарное содержимое вложения.
        filename (str): имя файла в multipart-запросе.
        upload_type (str): тип загрузки MAX ("image", "file", "video", "audio").
        content_type (str): MIME-тип содержимого.

    Returns:
        str: media-токен загруженного вложения.

    Raises:
        RuntimeError: если в ответе нет URL для загрузки или media-токена.
        requests.HTTPError: при неуспешном HTTP-статусе любого из запросов.
    """
    headers = {"Authorization": token}

    # Шаг 1: запрос URL для загрузки.
    print(f"[DEBUG] Запрос URL загрузки MAX (type={upload_type})...", file=sys.stderr)
    resp = requests.post(
        f"{MAX_API}/uploads",
        params={"type": upload_type},
        headers=headers,
        timeout=30,
        verify=True,
    )
    resp.raise_for_status()
    upload_info = resp.json()

    upload_url = upload_info.get("url")
    if not upload_url:
        raise RuntimeError(f"Нет URL загрузки в ответе: {upload_info}")

    # Шаг 2: загрузка файла на полученный URL.
    upload_resp = requests.post(
        upload_url,
        files={"data": (filename, io.BytesIO(data), content_type)},
        timeout=60,
        verify=True,
    )
    upload_resp.raise_for_status()
    upload_result = upload_resp.json()

    media_token = upload_result.get("token")
    if not media_token and "photos" in upload_result:
        for _photo_id, photo_data in upload_result["photos"].items():
            media_token = photo_data.get("token")
            if media_token:
                break

    if not media_token:
        raise RuntimeError(f"Нет токена во ответе загрузки: {upload_result}")

    return media_token


def _upload_image_to_max(token: str, png_bytes: bytes, filename: str) -> str:
    """Загружает изображение в MAX и возвращает media-токен.

    Обёртка над _upload_to_max с типом "image".

    Args:
        token (str): токен авторизации бота MAX.
        png_bytes (bytes): бинарное содержимое изображения (PNG/JPG).
        filename (str): имя файла при загрузке.

    Returns:
        str: media-токен изображения.
    """
    return _upload_to_max(token, png_bytes, filename, "image", "image/jpeg" if filename.lower().endswith((".jpg", ".jpeg")) else "image/png")


def _extract_mid(result: dict) -> str | None:
    """Извлекает mid отправленного сообщения из ответа POST /messages.

    Формат ответа MAX заранее не фиксирован, поэтому mid ищется терпимо в
    нескольких возможных местах.

    Args:
        result (dict): разобранный JSON-ответ отправки сообщения.

    Returns:
        str | None: mid сообщения либо None, если не найден.
    """
    if not isinstance(result, dict):
        return None
    message = result.get("message") if isinstance(result.get("message"), dict) else result
    body = message.get("body") if isinstance(message.get("body"), dict) else message
    for key in ("mid", "message_id", "messageId"):
        value = body.get(key) or message.get(key) or result.get(key)
        if value:
            return str(value)
    return None


def _send_max_message(
    token: str,
    target: dict,
    text: str,
    attachments: list | None = None,
    reply_to_mid: str | None = None,
) -> dict:
    """Отправляет сообщение в MAX (POST /messages) с текстом, вложениями и reply.

    Адресат передаётся query-параметрами, тело — JSON. Если задан reply_to_mid,
    сообщение отправляется как ответ на сообщение с этим mid (поле link).

    Args:
        token (str): токен авторизации бота MAX.
        target (dict): адресат как query-параметры: {"user_id": ...} или
            {"chat_id": ...}.
        text (str): текст сообщения (может быть пустым при наличии вложений).
        attachments (list | None): список вложений в формате API MAX.
        reply_to_mid (str | None): mid сообщения, на которое это ответ; None —
            обычное сообщение.

    Returns:
        dict: разобранный JSON-ответ API MAX.

    Raises:
        requests.HTTPError: при неуспешном HTTP-статусе запроса.
    """
    headers = {"Authorization": token, "Content-Type": "application/json"}
    payload: dict = {"text": text}
    if attachments:
        payload["attachments"] = attachments
    if reply_to_mid:
        payload["link"] = {"type": "reply", "mid": reply_to_mid}

    resp = requests.post(
        f"{MAX_API}/messages",
        params=target,
        headers=headers,
        json=payload,
        timeout=30,
        verify=True,
    )
    resp.raise_for_status()
    return resp.json()


def send_text_to_max(token: str, target: dict, text: str, reply_to_mid: str | None = None) -> str | None:
    """Отправляет текстовое сообщение в MAX и возвращает его mid.

    Args:
        token (str): токен авторизации бота MAX.
        target (dict): адресат: {"user_id": ...} или {"chat_id": ...}.
        text (str): текст сообщения.
        reply_to_mid (str | None): mid исходного сообщения для ответа-reply.

    Returns:
        str | None: mid отправленного сообщения, либо None при ошибке.
    """
    try:
        result = _send_max_message(token, target, text, reply_to_mid=reply_to_mid)
        return _extract_mid(result)
    except Exception as exc:
        print(f"[ERROR] Не удалось отправить текст в MAX {target}: {exc}", file=sys.stderr)
        return None


def send_file_to_max(
    token: str,
    target: dict,
    data: bytes,
    filename: str,
    content_type: str = "application/octet-stream",
    caption: str = "",
    reply_to_mid: str | None = None,
) -> str | None:
    """Загружает файл и отправляет его сообщением в MAX; возвращает mid.

    Args:
        token (str): токен авторизации бота MAX.
        target (dict): адресат: {"user_id": ...} или {"chat_id": ...}.
        data (bytes): содержимое файла.
        filename (str): имя файла.
        content_type (str): MIME-тип файла.
        caption (str): подпись к файлу (текст сообщения).
        reply_to_mid (str | None): mid исходного сообщения для ответа-reply.

    Returns:
        str | None: mid отправленного сообщения, либо None при ошибке.
    """
    try:
        media_token = _upload_to_max(token, data, filename, "file", content_type)
        threading.Event().wait(UPLOAD_PROCESS_DELAY)
        attachments = [{"type": "file", "payload": {"token": media_token}}]
        result = _send_max_message(token, target, caption, attachments, reply_to_mid=reply_to_mid)
        return _extract_mid(result)
    except Exception as exc:
        print(f"[ERROR] Не удалось отправить файл в MAX {target}: {exc}", file=sys.stderr)
        return None


def forward_to_max_chat(
    token: str,
    chat_id: str,
    text: str = "",
    png_bytes: bytes | None = None,
    filename: str = "photo.jpg",
) -> str | None:
    """Пересылает текст и/или картинку в чат MAX; возвращает mid сообщения.

    Используется мостом Telegram → MAX. Если передан png_bytes, картинка
    сначала загружается (с паузой на обработку сервером) и прикладывается
    вложением. Если нет ни текста, ни вложения — ничего не отправляет и
    возвращает None. Все ошибки перехватываются и логируются в stderr.

    Args:
        token (str): токен авторизации бота MAX.
        chat_id (str): ID чата MAX — адресат.
        text (str): текст сообщения; по умолчанию пустая строка.
        png_bytes (bytes | None): содержимое картинки для вложения; None — без
            изображения.
        filename (str): имя файла для загрузки картинки; по умолчанию
            "photo.jpg".

    Returns:
        str | None: mid отправленного сообщения MAX при успехе, иначе None.
    """
    try:
        attachments = None
        if png_bytes:
            media_token = _upload_image_to_max(token, png_bytes, filename)
            threading.Event().wait(UPLOAD_PROCESS_DELAY)
            attachments = [{"type": "image", "payload": {"token": media_token}}]

        if not text and not attachments:
            return None

        result = _send_max_message(token, {"chat_id": chat_id}, text or "", attachments)
        mid = _extract_mid(result)
        print(f"[SUCCESS] Сообщение переслано в чат MAX {chat_id} (mid={mid})", file=sys.stderr)
        return mid
    except Exception as exc:
        print(f"[ERROR] Не удалось переслать в чат MAX {chat_id}: {exc}", file=sys.stderr)
        return None
