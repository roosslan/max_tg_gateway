import io
import sys
import threading
import urllib3

import requests

# Отключаем предупреждения о проверке SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

MAX_API = "https://platform-api2.max.ru"


def _upload_image_to_max(token: str, png_bytes: bytes, filename: str) -> str:
    """Загружает картинку в MAX и возвращает media-токен вложения.

    Делает два запроса: сначала запрашивает у API URL для загрузки
    (POST /uploads?type=image), затем POST-ом отправляет файл на полученный
    URL. Из ответа извлекает media-токен: сначала пробует поле `token`, а если
    его нет — ищет токен внутри `photos[photoId]['token']`. Этот токен затем
    подставляется во вложение сообщения.

    Args:
        token (str): токен авторизации бота MAX (заголовок Authorization).
        png_bytes (bytes): бинарное содержимое изображения (PNG) для загрузки.
        filename (str): имя файла, под которым картинка отправляется в
            multipart-запросе.

    Returns:
        str: media-токен загруженного вложения для использования в сообщении.

    Raises:
        RuntimeError: если в ответе нет URL для загрузки или media-токена.
        requests.HTTPError: при неуспешном HTTP-статусе любого из запросов.
    """
    headers = {"Authorization": token}

    # Шаг 1: запрос URL для загрузки
    print("[DEBUG] Requesting MAX upload URL...", file=sys.stderr)
    resp = requests.post(
        f"{MAX_API}/uploads",
        params={"type": "image"},
        headers=headers,
        timeout=30,
        verify=False,
    )
    resp.raise_for_status()
    upload_info = resp.json()
    print(f"[DEBUG] MAX upload info response: {upload_info}", file=sys.stderr)

    upload_url = upload_info.get("url")
    if not upload_url:
        raise RuntimeError(f"No upload URL in response: {upload_info}")

    # Шаг 2: загрузка файла на полученный URL
    print(f"[DEBUG] Uploading to: {upload_url}", file=sys.stderr)
    upload_resp = requests.post(
        upload_url,
        files={"data": (filename, io.BytesIO(png_bytes), "image/png")},
        timeout=30,
        verify=False,
    )
    upload_resp.raise_for_status()
    upload_result = upload_resp.json()
    print(f"[DEBUG] MAX file upload response: {upload_result}", file=sys.stderr)

    # Токен может быть в photos[photoId]['token']
    media_token = upload_result.get("token")
    if not media_token and "photos" in upload_result:
        for photo_id, photo_data in upload_result["photos"].items():
            media_token = photo_data.get("token")
            if media_token:
                break

    if not media_token:
        raise RuntimeError(f"No token in upload response: {upload_result}")

    print(f"[DEBUG] Got media token: {media_token[:20]}...", file=sys.stderr)
    return media_token


def _send_max_message(token: str, target: dict, text: str, attachments: list | None = None) -> dict:
    """Отправляет сообщение в MAX (POST /messages) с текстом и вложениями.

    Формирует JSON-тело из текста и, если переданы, вложений, а адресата
    передаёт как query-параметры. Универсальна для личных диалогов и групповых
    чатов — конкретный адресат задаётся через target.

    Args:
        token (str): токен авторизации бота MAX (заголовок Authorization).
        target (dict): параметры адресата, передаются как query-параметры:
            {"user_id": ...} для личного диалога или {"chat_id": ...} для
            группового чата.
        text (str): текст сообщения (может быть пустым, если есть вложения).
        attachments (list | None): список вложений в формате API MAX
            (например [{"type": "image", "payload": {"token": ...}}]);
            None — сообщение без вложений.

    Returns:
        dict: разобранный JSON-ответ API MAX на отправку сообщения.

    Raises:
        requests.HTTPError: при неуспешном HTTP-статусе запроса.
    """
    headers = {"Authorization": token, "Content-Type": "application/json"}
    payload: dict = {"text": text}
    if attachments:
        payload["attachments"] = attachments

    print(f"[DEBUG] Sending MAX message to {target}...", file=sys.stderr)
    resp = requests.post(
        f"{MAX_API}/messages",
        params=target,
        headers=headers,
        json=payload,
        timeout=30,
        verify=False,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"[DEBUG] MAX send response: {result}", file=sys.stderr)
    return result


def forward_to_max_chat(
    token: str,
    chat_id: str,
    text: str = "",
    png_bytes: bytes | None = None,
    filename: str = "photo.jpg",
) -> bool:
    """Пересылает текст и/или картинку в групповой чат MAX (по chat_id).

    Используется мостом Telegram → MAX. Если передан png_bytes, картинка
    сначала загружается через _upload_image_to_max (с паузой 3 сек на
    обработку сервером) и превращается во вложение. Если нет ни текста, ни
    вложения — ничего не отправляет и возвращает False. Иначе отправляет
    сообщение в групповой чат по chat_id. Все ошибки перехватываются и
    логируются в stderr.

    Args:
        token (str): токен авторизации бота MAX.
        chat_id (str): ID группового чата MAX — адресат.
        text (str): текст сообщения; по умолчанию пустая строка.
        png_bytes (bytes | None): бинарное содержимое картинки (PNG/JPG) для
            вложения; None — сообщение без изображения.
        filename (str): имя файла для загрузки картинки; по умолчанию
            "photo.jpg".

    Returns:
        bool: True при успешной отправке, False при ошибке или когда нечего
            отправлять.
    """
    try:
        attachments = None
        if png_bytes:
            media_token = _upload_image_to_max(token, png_bytes, filename)
            # Пауза для обработки файла на сервере
            threading.Event().wait(3)
            attachments = [{"type": "image", "payload": {"token": media_token}}]

        if not text and not attachments:
            return False

        _send_max_message(token, {"chat_id": chat_id}, text or "", attachments)
        print(f"[SUCCESS] Message forwarded to MAX chat {chat_id}", file=sys.stderr)
        return True
    except Exception as exc:
        print(f"[ERROR] Failed to forward to MAX chat {chat_id}: {exc}", file=sys.stderr)
        return False
