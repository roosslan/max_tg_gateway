import sys

from config import load_config
from tg_bridge import run_bridge

APP_NAME = "max_tg_gateway"


def main() -> None:
    """Точка входа приложения: загружает конфиг и запускает мост Telegram → MAX.

    Загружает конфигурацию из ini.toml (при ошибке завершает процесс кодом 1).
    Если мост включён, запускает его основной цикл (блокирующий long polling)
    в текущем потоке и удерживает процесс живым до Ctrl+C. Если мост в конфиге
    выключен, просто завершает работу.

    Параметров нет (настройки читаются из файла конфигурации).

    Returns:
        None.

    Raises:
        SystemExit: с кодом 1, если конфигурацию не удалось загрузить.
    """
    try:
        config = load_config()
    except RuntimeError:
        sys.exit(1)

    if not config.tg_enabled:
        return

    try:
        run_bridge(config)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
