from __future__ import annotations

import unittest

from douk_manager.core.profile_url import (
    ProfileUrlError,
    normalize_url_for_compare,
    strict_admit_url,
)
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


if __name__ == "__main__":
    unittest.main()
