import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
from logging.handlers import RotatingFileHandler

from config import Config

_LOG_DIR = str(Config.LOG_DIR)
_MAX_LOG_SIZE = 5 * 1024 * 1024
_BACKUP_COUNT = 3
_IST = timezone(timedelta(hours=5, minutes=30))

os.makedirs(_LOG_DIR, exist_ok=True)


class _Formatter(logging.Formatter):
    _COLORS = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    _RESET = "\033[0m"

    def __init__(self, fmt=None, datefmt=None, use_color: bool = False):
        super().__init__(fmt=fmt, datefmt=datefmt)
        self.use_color = use_color

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, _IST).strftime(
            "%d%m%y %H:%M:%S"
        )

    def format(self, record):
        record = logging.makeLogRecord(record.__dict__.copy())
        if self.use_color:
            color = self._COLORS.get(record.levelno, "")
            record.levelname = f"{color}{record.levelname}{self._RESET}" if color else record.levelname
        record.filename = os.path.splitext(record.filename)[0]
        return super().format(record)


def _create_file_handler(filename: str):
    handler = RotatingFileHandler(
        os.path.join(_LOG_DIR, filename),
        maxBytes=_MAX_LOG_SIZE,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_Formatter(
        "%(asctime)s [%(levelname)s] | %(filename)s | %(message)s",
        use_color=False,
    ))
    return handler


def _create_console_handler():
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_Formatter(
        "%(asctime)s [%(levelname)s] | %(filename)s | %(message)s",
        use_color=True,
    ))
    return handler


def _configure_logger(name: str, filename: str, level: int):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if logger.handlers:
        return logger

    logger.addHandler(_create_file_handler(filename))

    if Config.DEBUG:
        logger.addHandler(_create_console_handler())

    return logger


app_logger = _configure_logger(
    "app_logger",
    "app.log",
    logging.DEBUG if Config.DEBUG else logging.INFO,
)

perf_logger = _configure_logger(
    "perf_logger",
    "performance.log",
    logging.INFO,
)


class track_performance:
    """
    Performance tracker usable as both a decorator and a context manager.

    Usage as Decorator:
        @track_performance("my_task")
        def run(): ...

    Usage as Context Manager:
        with track_performance("inline_task"):
            ...
    """

    def __init__(self, operation_name: str):
        self.operation_name = operation_name
        self.start_time: float = 0.0

    def __enter__(self):
        self.start_time = time.perf_counter()
        app_logger.debug("Started | operation=%s", self.operation_name)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = time.perf_counter() - self.start_time
        if exc_type is None:
            perf_logger.info(
                "Completed | operation=%s | duration=%.4fs",
                self.operation_name,
                duration,
            )
        else:
            perf_logger.error(
                "Failed | operation=%s | duration=%.4fs",
                self.operation_name,
                duration,
            )
            app_logger.exception("Operation failed | operation=%s", self.operation_name)
        return False  # Propagate exceptions

    def __call__(self, func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            with self.__class__(self.operation_name):
                return func(*args, **kwargs)
        return wrapper