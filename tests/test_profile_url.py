from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from douk_manager.core.profile_url import (
    FormalAccountRef,
    ProfileUrlError,
    ProfileUrlResolver,
    ProfileUrlStatus,
    normalize_url_for_compare,
    strict_admit_url,
)
from douk_manager.controller import ManagerController
from douk_manager.operation import OperationContext
from douk_manager.startup import StartupState
from douk_manager.vendor.collector_server import normalize_url_for_compare as vendor_compare


class ProfileUrlTests(unittest.TestCase):
    def test_loose_comparison_golden_cases_are_shared_with_vendor(self) -> None:
        cases = {
            "https://douyin.com:8443/user/x": "https://www.douyin.com/user/x",
            "https://www.douyin.com/user/a/b/?from=old#fragment": "https://www.douyin.com/user/a/b",
            "https://user@www.douyin.com/user/x": "https://user@www.douyin.com/user/x",
            "not-a-url/?q=1#x": "not-a-url",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_url_for_compare(raw), expected)
                self.assertEqual(vendor_compare(raw), expected)

    def test_strict_gate_accepts_only_one_profile_segment_and_default_ports(self) -> None:
        self.assertEqual(
            strict_admit_url("http://douyin.com:80/user/x?from=test#top"),
            "https://www.douyin.com/user/x",
        )
        self.assertEqual(
            strict_admit_url("https://www.douyin.com/user/x/"),
            "https://www.douyin.com/user/x",
        )
        rejected = (
            "https://douyin.com:8443/user/x",
            "https://user@www.douyin.com/user/x",
            "https://www.douyin.com/user/a/b",
            "https://www.douyin.com/user/a%2Fb",
            "https://www.douyin.com/user/a\\b",
            "https://www.douyin.com/user/",
            "https://v.douyin.com/user/x",
            "https://www.douyin.com/user/%zz",
        )
        for raw in rejected:
            with self.subTest(raw=raw), self.assertRaises(ProfileUrlError):
                strict_admit_url(raw)

    def test_formal_resolver_uses_current_a_position_and_fixed_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "settings_master.json"
            master.write_text(
                json.dumps(
                    {
                        "accounts_urls": [
                            {"url": "https://douyin.com/user/one?from=test"},
                            {"url": "https://www.douyin.com/user/two"},
                            {"url": "https://www.douyin.com/user/three/extra"},
                            {"url": ""},
                            {"url": 12},
                            {"url": "https://www.douyin.com/user/six"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            resolver = ProfileUrlResolver(master, lock_path=root / "settings.lock")

            found = resolver.resolve(FormalAccountRef(1))
            self.assertEqual(found.status, ProfileUrlStatus.FOUND)
            self.assertEqual(found.url, "https://www.douyin.com/user/one")
            self.assertEqual(
                resolver.resolve(FormalAccountRef(2)).url,
                "https://www.douyin.com/user/two",
            )
            self.assertEqual(
                resolver.resolve(FormalAccountRef(3)).status,
                ProfileUrlStatus.INVALID,
            )
            self.assertEqual(
                resolver.resolve(FormalAccountRef(4)).status,
                ProfileUrlStatus.MISSING,
            )
            self.assertEqual(
                resolver.resolve(FormalAccountRef(5)).status,
                ProfileUrlStatus.MISSING,
            )
            self.assertEqual(
                resolver.resolve(FormalAccountRef(6)).url,
                "https://www.douyin.com/user/six",
            )
            self.assertEqual(
                resolver.resolve(FormalAccountRef(7)).status,
                ProfileUrlStatus.MISSING,
            )
            self.assertEqual(
                resolver.resolve(FormalAccountRef(True)).status,
                ProfileUrlStatus.MISSING,
            )

    def test_formal_resolver_does_not_expose_read_or_shape_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = ProfileUrlResolver(root / "missing.json")
            self.assertEqual(
                missing.resolve(FormalAccountRef(1)).status,
                ProfileUrlStatus.READ_ERROR,
            )

            malformed = root / "malformed.json"
            malformed.write_text("[]", encoding="utf-8")
            self.assertEqual(
                ProfileUrlResolver(malformed).resolve(FormalAccountRef(1)).status,
                ProfileUrlStatus.READ_ERROR,
            )

    def test_controller_forwards_formal_reference_without_reading_ui_data(self) -> None:
        controller = ManagerController.__new__(ManagerController)
        controller.startup_state = StartupState.READY
        resolver = Mock()
        expected = object()
        resolver.resolve.return_value = expected
        controller.profile_url_resolver = resolver

        result = controller.resolve_profile_url(7, context=OperationContext())

        self.assertIs(result, expected)
        resolver.resolve.assert_called_once_with(FormalAccountRef(7))


if __name__ == "__main__":
    unittest.main()
