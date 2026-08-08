from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from douk_manager.integrations.screenshots import ScreenshotService


class ScreenshotTests(unittest.TestCase):
    def test_safe_move_to_authoritative_account_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "Inbox"
            accounts = root / "Accounts"
            destination = accounts / "UID123456_A15display_A999_发布作品"
            inbox.mkdir()
            destination.mkdir(parents=True)
            source = inbox / "A15.jpg"
            source.write_bytes(b"safe screenshot bytes")
            service = ScreenshotService()
            preview = service.preview(inbox, accounts)
            self.assertEqual(preview.movable, 1)
            result = service.execute(inbox, accounts)
            self.assertEqual(result.moved, 1)
            self.assertFalse(source.exists())
            self.assertEqual((destination / "A15.jpg").read_bytes(), b"safe screenshot bytes")


if __name__ == "__main__":
    unittest.main()

