import sys
import threading

from config import load_config
from max_receiver import run_receiver
from store import MappingStore
from tg_bridge import run_bridge

APP_NAME = "max_tg_gateway"


def main() -> None:
    """Точка входа: загружает конфиг и запускает мост и приём MAX.

    Загружает конфигурацию из ini.toml (при ошибке завершает процесс кодом 1),
    создаёт общее хранилище соответствий и запускает до двух фоновых циклов:
    мост Telegram → MAX (если включён в конфиге) и приём входящих MAX (если
    задан токен MAX). Удерживает процесс живым, пока работают циклы, до Ctrl+C.

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

    store = MappingStore(config.state_file)

    threads: list[threading.Thread] = []

    if config.tg_enabled:
        threads.append(
            threading.Thread(target=run_bridge, args=(config, store), daemon=True)
        )

    if config.max_token:
        threads.append(
            threading.Thread(target=run_receiver, args=(config, store), daemon=True)
        )

    if not threads:
        return

    for thread in threads:
        thread.start()

    # Держим процесс живым, пока живы фоновые циклы (до Ctrl+C).
    try:
        for thread in threads:
            while thread.is_alive():
                thread.join(timeout=1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
