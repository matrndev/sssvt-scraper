import logging
import os
import time
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path


@contextmanager
def logged_run(name):
    directory = Path(os.environ.get("LOG_DIR") or Path(__file__).resolve().parents[1] / "logs")
    directory.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        directory / f"{name}.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    formatter = logging.Formatter(
        "%(asctime)sZ %(levelname)s pid=%(process)d %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime
    for handler in (file_handler, console):
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console], force=True)
    logger = logging.getLogger(name)
    started = time.monotonic()
    logger.info("Run started")
    try:
        yield logger
    except Exception:
        logger.exception("Run failed after %.2fs", time.monotonic() - started)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        logger.error("Run interrupted after %.2fs", time.monotonic() - started)
        raise SystemExit(130) from None
    else:
        logger.info("Run succeeded in %.2fs", time.monotonic() - started)
    finally:
        logging.shutdown()
