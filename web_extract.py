"""Извлечение содержимого по ссылке: файл или читаемый текст веб-страницы.

По URL определяется тип содержимого:
- не-HTML (pdf, картинка, архив и т.п.) — считается файлом: скачивается и
  возвращается как байты для пересылки вложением;
- HTML-страница — из неё извлекается читаемый текст (заголовок + основной
  текст статьи) через библиотеку trafilatura.

Тяжёлый headless-браузер намеренно не используется — модуль рассчитан на
лёгкое окружение.
"""
import re
import sys
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import requests

# Префикс User-Agent обычного браузера — некоторые сайты отдают контент только так.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Ограничение размера скачиваемого файла (байт), чтобы не выгрести гигабайты.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

# Регулярка для поиска первой ссылки в тексте сообщения.
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


@dataclass
class FetchResult:
    """Результат обработки ссылки.

    Attributes:
        kind (str): "text" — извлечён читаемый текст; "file" — скачан файл;
            "error" — не удалось обработать.
        title (str): заголовок страницы (для kind="text").
        text (str): читаемый текст (для kind="text") или текст ошибки
            (для kind="error").
        data (bytes | None): содержимое файла (для kind="file").
        filename (str): имя файла (для kind="file").
        content_type (str): MIME-тип файла (для kind="file").
    """
    kind: str
    title: str = ""
    text: str = ""
    data: bytes | None = None
    filename: str = ""
    content_type: str = ""


def find_first_url(text: str) -> str | None:
    """Находит первую http(s)-ссылку в тексте сообщения.

    Args:
        text (str): текст, в котором ищется ссылка.

    Returns:
        str | None: первая найденная ссылка либо None, если ссылок нет.
    """
    if not text:
        return None
    match = URL_RE.search(text)
    return match.group(0) if match else None


def _filename_from_url(url: str, content_type: str) -> str:
    """Подбирает имя файла из пути URL; при отсутствии — по MIME-типу.

    Args:
        url (str): исходная ссылка.
        content_type (str): значение заголовка Content-Type.

    Returns:
        str: имя файла с расширением.
    """
    path = urlparse(url).path
    name = unquote(path.rsplit("/", 1)[-1]) if path else ""
    if name and "." in name:
        return name
    # Фолбэк: базовое имя + расширение из типа содержимого.
    ext = ""
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "application/zip": ".zip",
        "text/plain": ".txt",
    }
    ext = mapping.get(ct, "")
    return (name or "file") + ext


def fetch_url(url: str) -> FetchResult:
    """Скачивает ссылку и возвращает либо читаемый текст, либо файл.

    Делает потоковый GET-запрос, по заголовку Content-Type решает: HTML-страница
    или файл. Для HTML извлекает читаемый текст через trafilatura. Для файла
    скачивает содержимое (с ограничением размера). При ошибке возвращает
    результат с kind="error".

    Args:
        url (str): ссылка для обработки.

    Returns:
        FetchResult: результат обработки (text/file/error).
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
            stream=True,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as exc:
        print(f"[ERROR] Не удалось загрузить {url}: {exc}", file=sys.stderr)
        return FetchResult(kind="error", text=f"Не удалось загрузить ссылку: {exc}")

    content_type = resp.headers.get("Content-Type", "")
    is_html = "text/html" in content_type.lower() or "application/xhtml" in content_type.lower()

    # Читаем тело с ограничением по размеру.
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                resp.close()
                if is_html:
                    break  # для текста оборванного HTML обычно достаточно
                return FetchResult(
                    kind="error",
                    text=f"Файл слишком большой (> {MAX_DOWNLOAD_BYTES // (1024 * 1024)} МБ).",
                )
    except Exception as exc:
        print(f"[ERROR] Ошибка чтения тела {url}: {exc}", file=sys.stderr)
        return FetchResult(kind="error", text=f"Ошибка чтения содержимого: {exc}")
    finally:
        resp.close()

    body = b"".join(chunks)

    if not is_html:
        filename = _filename_from_url(url, content_type)
        return FetchResult(
            kind="file",
            data=body,
            filename=filename,
            content_type=(content_type.split(";")[0].strip() or "application/octet-stream"),
        )

    # HTML — извлекаем читаемый текст.
    html = body.decode(resp.encoding or "utf-8", errors="replace")
    title, text = _extract_readable(html, url)
    if not text:
        return FetchResult(
            kind="error",
            text="Не удалось извлечь читаемый текст со страницы.",
        )
    return FetchResult(kind="text", title=title, text=text)


def _extract_readable(html: str, url: str) -> tuple[str, str]:
    """Извлекает заголовок и основной текст из HTML через trafilatura.

    Args:
        html (str): исходный HTML страницы.
        url (str): ссылка (помогает trafilatura с относительными путями).

    Returns:
        tuple[str, str]: (заголовок, читаемый текст). Пустые строки, если
            извлечь не удалось.
    """
    try:
        import trafilatura
    except Exception as exc:
        print(f"[ERROR] trafilatura недоступна: {exc}", file=sys.stderr)
        return "", ""

    try:
        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
        ) or ""
    except Exception as exc:
        print(f"[ERROR] Ошибка извлечения текста: {exc}", file=sys.stderr)
        text = ""

    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        if meta and getattr(meta, "title", None):
            title = meta.title
    except Exception:
        title = ""

    return title, text.strip()
