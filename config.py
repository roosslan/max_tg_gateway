import os
import tomllib
from dataclasses import dataclass, field

import constconf

CONFIG_FILENAME = "ini.toml"
STATE_FILENAME = "state.json"


@dataclass
class Config:
    # Токен бота MAX — нужен мосту для отправки в чаты MAX и приёму входящих.
    max_token: str = ""
    # MAX ID владельца — от него принимаются ссылки для чтения.
    max_owner_id: str = ""
    # Мост Telegram -> MAX
    tg_enabled: bool = False
    tg_token: str = ""
    # Соответствие: id отправителя/чата в Telegram -> id чата MAX
    tg_to_max: dict[str, str] = field(default_factory=dict)
    # Путь к файлу хранилища соответствий (mid MAX -> сообщение Telegram)
    state_file: str = ""


def app_dir() -> str:
    """Возвращает путь к директории, где находится модуль приложения.

    Нужна, чтобы искать ini.toml и файл состояния рядом с приложением.

    Параметров нет.

    Returns:
        str: абсолютный путь к директории приложения.
    """
    return os.path.dirname(os.path.abspath(__file__))


def load_config() -> Config:
    """Читает ini.toml рядом с приложением и собирает объект Config.

    Разбирает секции [max], [telegram] и таблицу [tg_to_max]. Из [max] берутся
    токен бота (нужен и мосту, и приёму входящих) и owner_id (MAX ID владельца
    для фичи чтения ссылок); при пустом токене подставляется constconf.TESTT.
    Мост Telegram → MAX считается включённым, если [telegram] on=yes и задан
    токен бота. Проверяет, что доступен хотя бы один режим работы: мост или
    приём входящих MAX.

    Параметров нет (путь к файлу определяется автоматически).

    Returns:
        Config: заполненная конфигурация приложения.

    Raises:
        RuntimeError: если файл конфигурации не найден, не парсится как TOML,
            либо не настроен ни один режим (ни мост, ни токен MAX).
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
    config.state_file = os.path.join(app_dir(), STATE_FILENAME)

    # MAX section — токен бота и MAX ID владельца.
    max_section = data.get("max")
    if isinstance(max_section, dict):
        max_token = str(max_section.get("token", "")).strip()
        if not max_token:
            max_token = constconf.TESTT
        config.max_token = max_token
        config.max_owner_id = str(max_section.get("owner_id", "")).strip()

    # Telegram section (мост Telegram -> MAX)
    tg_section = data.get("telegram")
    if isinstance(tg_section, dict):
        tg_enabled = str(tg_section.get("on", "no")).strip().lower() in ("yes", "true", "1")
        tg_token = str(tg_section.get("token", "")).strip()
        if tg_enabled and tg_token:
            config.tg_enabled = True
            config.tg_token = tg_token

    # Таблица соответствия tg_to_max: "TG_ID" = "ID_чата_MAX"
    tg_map = data.get("tg_to_max")
    if isinstance(tg_map, dict):
        config.tg_to_max = {
            str(key).strip(): str(value).strip()
            for key, value in tg_map.items()
            if str(key).strip() and str(value).strip()
        }

    # Нужен хотя бы один режим: мост Telegram→MAX или приём входящих MAX.
    if not config.tg_enabled and not config.max_token:
        raise RuntimeError(
            f"В {CONFIG_FILENAME} не настроен ни один режим: нужен либо "
            f"[telegram] с on=yes и токеном, либо токен [max] token для приёма MAX"
        )

    return config
