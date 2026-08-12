from __future__ import annotations

import re
import unittest
from pathlib import Path

from douk_manager import __version__


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.1.2"


class VersionIdentityTests(unittest.TestCase):
    def test_package_runtime_and_build_info_versions_match(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        workflow = (
            ROOT / ".github" / "workflows" / "build-windows.yml"
        ).read_text(encoding="utf-8")

        package_version = re.search(
            r'(?m)^version = "([^"]+)"$', pyproject
        )
        build_version = re.search(
            r'(?m)^\s*"Version: ([^"]+)"$', workflow
        )

        self.assertIsNotNone(package_version)
        self.assertIsNotNone(build_version)
        self.assertEqual(package_version.group(1), EXPECTED_VERSION)
        self.assertEqual(__version__, EXPECTED_VERSION)
        self.assertEqual(build_version.group(1), EXPECTED_VERSION)


if __name__ == "__main__":
    unittest.main()
