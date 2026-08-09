from __future__ import annotations

import unittest
from unittest.mock import patch

from douk_manager.vendor.collector_server import CollectorError, format_error_for_console


class CollectorLoggingTests(unittest.TestCase):
    def test_unencodable_diagnostic_never_interrupts_error_handling(self) -> None:
        written: list[str] = []

        def gbk_like_print(message: str) -> None:
            if "✓" in message:
                raise UnicodeEncodeError("gbk", "✓", 0, 1, "illegal multibyte sequence")
            written.append(message)

        error = CollectorError(
            "DUPLICATE_JSON_URL",
            "账号重复，未写入。",
            details={"category_summary": "✓ 已分类"},
        )
        with patch("builtins.print", side_effect=gbk_like_print):
            format_error_for_console(error)

        self.assertEqual(len(written), 2)
        self.assertIn("DUPLICATE_JSON_URL", written[0])
        self.assertIn("\\u2713", written[1])


if __name__ == "__main__":
    unittest.main()
