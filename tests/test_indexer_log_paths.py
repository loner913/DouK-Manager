from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from douk_manager.controller import ManagerController
from douk_manager.integrations.indexer import IndexResult, IndexService


def _summary(mode: str) -> str:
    summary = {
        "Mode": mode,
        "SourceRoot": "synthetic-source",
        "IndexRoot": "synthetic-index",
        "SourceFoldersScanned": 0,
        "IgnoredSourceFolders": 0,
        "EmptySourceFolders": 0,
        "MovedOrDeletedTargetFolders": 0,
        "EmptySourceShortcutsDetected": 0,
        "MissingTargetShortcutsDetected": 0,
        "PlannedShortcutDeletions": 0,
        "DeletedEmptySourceShortcuts": 0,
        "DeletedMissingTargetShortcuts": 0,
        "DeletedShortcutsTotal": 0,
        "ShortcutDeleteFailures": 0,
        "RemainingEmptySourceShortcuts": 0,
        "RemainingMissingTargetShortcuts": 0,
        "ShortcutReadFailures": 0,
    }
    if mode == "Refresh":
        summary.update(Created=0, Updated=0, Unchanged=0, IndexFailures=0)
    return "DOUK_INDEX_SUMMARY_JSON=" + json.dumps(summary)


class IndexServiceLogPathTests(unittest.TestCase):
    def _assert_log_root_argument(self, operation: str) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            index = root / "index"
            log_root = root / "logs" / operation
            script = root / f"{operation}.ps1"
            source.mkdir()
            script.write_text("# synthetic test script\n", encoding="utf-8")
            mode = "Refresh" if operation == "refresh" else "ManualCleanup"
            completed = SimpleNamespace(
                returncode=0,
                stdout=_summary(mode),
                stderr="",
            )

            with (
                patch("douk_manager.integrations.indexer.os.name", "nt"),
                patch(
                    "douk_manager.integrations.indexer.resource_path",
                    return_value=script,
                ),
                patch(
                    "douk_manager.integrations.indexer.subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                service = IndexService()
                if operation == "refresh":
                    service.refresh(source, index, log_root)
                else:
                    service.cleanup(source, index, log_root)

            command = run.call_args.args[0]
            self.assertEqual(command.count("-LogRoot"), 1)
            argument_index = command.index("-LogRoot")
            self.assertEqual(command[argument_index + 1], str(log_root))
            self.assertTrue(log_root.is_dir())

    def test_refresh_command_receives_explicit_log_root(self) -> None:
        self._assert_log_root_argument("refresh")

    def test_cleanup_command_receives_explicit_log_root(self) -> None:
        self._assert_log_root_argument("cleanup")


class ControllerIndexLogPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = ManagerController.__new__(ManagerController)
        self.controller.paths = SimpleNamespace(
            video_root=Path("synthetic-video-root"),
            index_root=Path("synthetic-index-root"),
            index_refresh_logs=Path("synthetic-logs/IndexRefresh"),
            index_cleanup_logs=Path("synthetic-logs/IndexCleanup"),
        )
        self.controller.indexer = Mock()
        self.controller.logger = Mock()
        self.controller.indexer.refresh.return_value = IndexResult(0, "")
        self.controller.indexer.cleanup.return_value = IndexResult(0, "")

    def test_refresh_index_forwards_refresh_log_directory(self) -> None:
        self.controller.refresh_index()

        self.controller.indexer.refresh.assert_called_once_with(
            self.controller.paths.video_root,
            self.controller.paths.index_root,
            self.controller.paths.index_refresh_logs,
        )

    def test_cleanup_index_forwards_cleanup_log_directory(self) -> None:
        self.controller.cleanup_index()

        self.controller.indexer.cleanup.assert_called_once_with(
            self.controller.paths.video_root,
            self.controller.paths.index_root,
            self.controller.paths.index_cleanup_logs,
        )


if __name__ == "__main__":
    unittest.main()
