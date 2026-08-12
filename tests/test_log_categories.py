from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from douk_manager.logging_setup import setup_logging
from tests.helpers import make_test_paths


class LogCategoryTests(unittest.TestCase):
    def test_managed_paths_create_five_log_categories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory))

            expected = {
                paths.manager_logs: "Manager",
                paths.collector_logs: "Collector",
                paths.download_task_logs: "DownloadTasks",
                paths.index_refresh_logs: "IndexRefresh",
                paths.index_cleanup_logs: "IndexCleanup",
            }
            for path, name in expected.items():
                self.assertEqual(path, paths.logs / name)
                self.assertTrue(path.is_dir())
            self.assertFalse(any(path.is_file() for path in paths.logs.iterdir()))

    def test_manager_log_uses_manager_category_and_existing_name_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory))

            logger, log_path = setup_logging(paths.manager_logs)
            try:
                self.assertEqual(log_path.parent, paths.manager_logs)
                self.assertRegex(
                    log_path.name,
                    r"^DouKManager_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6}\.log$",
                )
            finally:
                for handler in logger.handlers[:]:
                    handler.close()
                    logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
