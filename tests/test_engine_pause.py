from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from douk_manager.config import AppConfig
from douk_manager.core.backup import BackupService
from douk_manager.core.engine import EngineService
from tests.helpers import make_test_paths


class EnginePauseTests(unittest.TestCase):
    def test_pause_wrapper_preserves_exit_code_and_waits_for_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            config = AppConfig(
                engine_exe=str(paths.engine_exe),
                video_root=str(paths.video_root),
                index_root=str(paths.index_root),
            )
            service = EngineService(paths, config, BackupService(paths))
            wrapper = service._write_pause_wrapper()
            content = wrapper.read_text(encoding="ascii")
            self.assertIn(f'call "{paths.engine_exe}"', content)
            self.assertIn('set "DOUK_ENGINE_EXIT=%ERRORLEVEL%"', content)
            self.assertIn("pause >nul", content)
            self.assertIn("exit /b %DOUK_ENGINE_EXIT%", content)


if __name__ == "__main__":
    unittest.main()
