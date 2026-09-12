from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path


def setup_logging(log_dir: Path) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"DouKManager_{datetime.now():%Y-%m-%d_%H-%M-%S-%f}.log"
    logger = logging.getLogger("douk_manager")
    logger.setLevel(logging.INFO)
    # Removing a FileHandler without closing it leaves the log file locked on
    # Windows. Close old handlers before installing the handler for this run.
    for old_handler in list(logger.handlers):
        logger.removeHandler(old_handler)
        old_handler.close()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    logger.info("DouK 全流程一体化管理器启动")
    return logger, path

