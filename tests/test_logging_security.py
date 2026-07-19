from __future__ import annotations

import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path

from bot.utils.logging_setup import _SecureRotatingFileHandler, configure_logging


class LoggingFilePermissionTests(unittest.TestCase):
    def test_default_file_log_is_written_under_persistent_data_directory(self) -> None:
        root = logging.getLogger()
        old_handlers = list(root.handlers)
        old_level = root.level
        old_cwd = Path.cwd()
        old_log_to_file = os.environ.get("LOG_TO_FILE")
        old_log_file_path = os.environ.get("LOG_FILE_PATH")
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                os.chdir(tmpdir)
                os.environ["LOG_TO_FILE"] = "true"
                os.environ.pop("LOG_FILE_PATH", None)
                configure_logging(force=True)
                logging.getLogger("default-log-path-test").info("persist me")
                for handler in root.handlers:
                    handler.flush()
                self.assertTrue((Path(tmpdir) / "data" / "bot.log").is_file())
            finally:
                for handler in list(root.handlers):
                    if handler not in old_handlers:
                        handler.close()
                root.handlers = old_handlers
                root.setLevel(old_level)
                os.chdir(old_cwd)
                if old_log_to_file is None:
                    os.environ.pop("LOG_TO_FILE", None)
                else:
                    os.environ["LOG_TO_FILE"] = old_log_to_file
                if old_log_file_path is None:
                    os.environ.pop("LOG_FILE_PATH", None)
                else:
                    os.environ["LOG_FILE_PATH"] = old_log_file_path

    def test_log_file_and_rollover_remain_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bot.log"
            handler = _SecureRotatingFileHandler(
                path,
                maxBytes=1,
                backupCount=1,
                encoding="utf-8",
            )
            logger = logging.getLogger(f"test-secure-log-{id(self)}")
            logger.handlers = [handler]
            logger.propagate = False
            logger.setLevel(logging.INFO)
            try:
                logger.info("first")
                handler.doRollover()
                logger.info("second")
            finally:
                handler.close()
                logger.handlers.clear()

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            backup = Path(f"{path}.1")
            self.assertTrue(backup.exists())
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
