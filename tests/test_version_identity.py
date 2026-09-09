from __future__ import annotations

import re
import unittest
from pathlib import Path

from douk_manager import __version__


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_VERSION = "0.1.6"


class VersionIdentityTests(unittest.TestCase):
    def test_package_runtime_and_build_info_versions_match(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        workflow = (
            ROOT / ".github" / "workflows" / "build-windows.yml"
        ).read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        release_notes = ROOT / f"RELEASE-NOTES-v{EXPECTED_VERSION}.md"

        package_version = re.search(
            r'(?m)^version = "([^"]+)"$', pyproject
        )
        build_version = re.search(
            r'(?m)^\s*"Version: ([^"]+)"$', workflow
        )
        artifact_name = re.search(
            r"(?m)^\s*name: (DouK-Manager_Windows_X64-.+)$", workflow
        )

        self.assertIsNotNone(package_version)
        self.assertIsNotNone(build_version)
        self.assertIsNotNone(artifact_name)
        self.assertEqual(package_version.group(1), EXPECTED_VERSION)
        self.assertEqual(__version__, EXPECTED_VERSION)
        self.assertEqual(build_version.group(1), EXPECTED_VERSION)
        self.assertIn(f"v{EXPECTED_VERSION}", artifact_name.group(1))
        self.assertIn("actions/upload-artifact@v7", workflow)
        self.assertIn("Get-FileHash -Algorithm SHA256", workflow)
        self.assertIn("DouK-Manager_Windows_X64.zip.sha256", workflow)
        self.assertIn(f"[v{EXPECTED_VERSION}](RELEASE-NOTES-v{EXPECTED_VERSION}.md)", readme)
        self.assertIn(f"## v{EXPECTED_VERSION}", changelog)
        self.assertTrue(release_notes.is_file())


if __name__ == "__main__":
    unittest.main()
