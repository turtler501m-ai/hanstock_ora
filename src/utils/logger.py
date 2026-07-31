import logging
import sys
from loguru import logger
from src.config import config
import os

LOG_LEVEL = os.environ.get("HANSTOCK_LOG_LEVEL", "INFO").upper()
LOG_ROTATION = os.environ.get("HANSTOCK_LOG_ROTATION", "5 MB")
LOG_RETENTION = os.environ.get("HANSTOCK_LOG_RETENTION", "14 days")
TESTING = os.environ.get("HANSTOCK_TESTING") == "1"

# Remove default handler
logger.remove()

# Add console handler when the process has an attached stream.
console_sink = sys.stdout or sys.stderr
if console_sink is not None:
    logger.add(
        console_sink,
        format="<green>{time:YYYY-MM-DD HH:mm:ss KST}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=LOG_LEVEL,
        colorize=True,
    )

# Add file handler with rotation
log_dir = os.path.dirname(config.log_file)
if log_dir:
    os.makedirs(log_dir, exist_ok=True)

if not TESTING:
    logger.add(
        config.log_file,
        format="{time:YYYY-MM-DD HH:mm:ss KST} | {level: <8} | {name}:{function}:{line} - {message}",
        level=LOG_LEVEL,
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        compression="zip",
        encoding="utf-8"
    )


class _InterceptHandler(logging.Handler):
    """Route standard-library logs through the project's Loguru sinks."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = logging.currentframe()
        depth = 2
        while frame is not None and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


# Some trading integrations use the standard logging module.  Intercepting the
# root logger keeps their order/execution records in the same trader.log file.
logging.basicConfig(
    handlers=[_InterceptHandler()],
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    force=True,
)
