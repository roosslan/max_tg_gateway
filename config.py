import os
import tomllib
from dataclasses import dataclass, field

import constconf

CONFIG_FILENAME = "ini.toml"

@dataclass
class Config:
    # Токен бота MAX — нужен мосту для отправки в чаты MAX.
    max_token: str = ""
    # Мост Telegram -> MAX
    tg_enabled: bool = False
    tg_token: str = ""
    # Соответствие: id отправителя в Telegram -> id группового чата MAX
    tg_to_max: dict[str, str] = field(default_factory=dict)


def app_dir() -> str:
    """Возвращает путь к директории, где находится запускаемый файл.

    Нужна, чтобы искать ini.toml рядом с приложением как в режиме исходного
    кода, так и в собранном PyInstaller-бинарнике. Если процесс «заморожен»
    (sys.frozen), берётся папка исполняемого файла (sys.executable); иначе —
    папка текущего .py-модуля.

    Параметров нет.

    Returns:
        str: абсолютный путь к директории приложения.
    """
    import sys
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def load_config() -> Config:
    """Читает ini.toml рядом с приложением и собирает объект Config.

    Находит файл конфигурации через app_dir(), парсит его как TOML и разбирает
    секции [max], [telegram] и таблицу [tg_to_max]. Из секции [max] берётся
    только токен бота (он нужен мосту для отправки в чаты MAX); при пустом
    токене подставляется constconf.TESTT. Мост Telegram → MAX считается
    включённым, если [telegram] on=yes (или true/1) и задан токен бота. В конце
    проверяет, что мост включён.

    Параметров нет (путь к файлу определяется автоматически).

    Returns:
        Config: заполненная конфигурация приложения.

    Raises:
        RuntimeError: если файл конфигурации не найден, не парсится как TOML,
            либо мост Telegram → MAX не включён ([telegram] с on=yes).
    """
    config_path = os.path.join(app_dir(), CONFIG_FILENAME)
    if not os.path.isfile(config_path):
        raise RuntimeError(f"Не найден файл конфигурации: {config_path}")

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"Ошибка разбора {CONFIG_FILENAME}: {exc}") from exc

    config = Config()

    # MAX section — из неё нужен только токен бота для отправки в чаты MAX.
    max_section = data.get("max")
    if isinstance(max_section, dict):
        max_token = str(max_section.get("token", "")).strip()

        # Если токен MAX не указан, используем TESTT из constconf
        if not max_token:
            max_token = constconf.TESTT

        config.max_token = max_token

    # Telegram section (мост Telegram -> MAX)
    tg_section = data.get("telegram")
    if isinstance(tg_section, dict):
        tg_enabled = str(tg_section.get("on", "no")).strip().lower() in ("yes", "true", "1")
        tg_token = str(tg_section.get("token", "")).strip()
        if tg_enabled and tg_token:
            config.tg_enabled = True
            config.tg_token = tg_token

    # Таблица соответствия tg_to_max: "TG_ID" = "ID_MAX_ЧАТА"
    tg_map = data.get("tg_to_max")
    if isinstance(tg_map, dict):
        config.tg_to_max = {
            str(key).strip(): str(value).strip()
            for key, value in tg_map.items()
            if str(key).strip() and str(value).strip()
        }

    # Мост — единственный канал; без него запускать нечего.
    if not config.tg_enabled:
        raise RuntimeError(
            f"В {CONFIG_FILENAME} не включён мост Telegram → MAX: "
            f"нужна секция [telegram] с on=yes и токеном бота"
        )

    return config
