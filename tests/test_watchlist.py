from __future__ import annotations

import copy
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from douk_manager.config import ManagedPaths
from douk_manager.core import watchlist as watchlist_module
from douk_manager.core.json_store import read_json, write_json_atomic
from douk_manager.core.watchlist import WatchlistError, WatchlistService, validate_control
from tests.helpers import make_test_paths


class WatchlistTests(unittest.TestCase):
    def _service(self, directory: str) -> tuple[ManagedPaths, WatchlistService]:
        paths = make_test_paths(Path(directory), account_count=2)
        service = WatchlistService(paths)
        service.initialize(initialization_evidence=True)
        service.mark_ready()
        return paths, service

    @staticmethod
    def _payload(url: str = "https://www.douyin.com/user/42") -> dict[str, object]:
        return {
            "request_id": str(uuid.uuid4()),
            "url": url,
            "captured_nickname": "合成昵称",
            "display_name": "合成名称",
            "douyin_id": "synthetic_42",
            "nickname_blank": False,
            "reasons": ["few_works"],
            "note": "synthetic observation",
            "review_after_days": 7,
        }

    def test_initialization_requires_evidence_and_does_not_reset_existing_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), account_count=2)
            service = WatchlistService(paths)
            with self.assertRaisesRegex(WatchlistError, "首次启用") as caught:
                service.initialize(initialization_evidence=False)
            self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
            self.assertFalse(paths.watchlist.exists())
            service.initialize(initialization_evidence=True)
            control = read_json(paths.watchlist_control)
            self.assertEqual(control["initialization_state"], "complete")
            self.assertEqual(control["write_gate"], "blocked")
            with self.assertRaises(WatchlistError) as repeated:
                service.initialize(initialization_evidence=True)
            self.assertEqual(repeated.exception.code, "INITIALIZATION_CONFLICT")

    def test_create_replay_keeps_identity_and_first_review_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload()
            created = service.observe(payload)
            self.assertEqual(created["status"], "CREATED")
            self.assertEqual(created["w_id"], 1)
            before = read_json(paths.watchlist)
            control = read_json(paths.watchlist_control)
            receipt = control["request_receipts"][0]
            self.assertEqual(receipt["outcome"], "committed")
            self.assertEqual(receipt["next_review_at"], before["records"][0]["next_review_at"])

            replay = service.observe(payload)
            self.assertEqual(replay["status"], "EXISTS")
            self.assertEqual(replay["w_id"], 1)
            self.assertEqual(read_json(paths.watchlist), before)
            self.assertEqual(read_json(paths.watchlist_control), control)

            changed = copy.deepcopy(payload)
            changed["display_name"] = "另一个名称"
            with self.assertRaises(WatchlistError) as conflict:
                service.observe(changed)
            self.assertEqual(conflict.exception.code, "REQUEST_CONFLICT")
            self.assertEqual(read_json(paths.watchlist), before)

    def test_formal_duplicate_uses_loose_compare_without_consuming_w(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload("http://douyin.com:80/user/1")
            result = service.observe(payload)
            self.assertEqual(result["status"], "FORMAL_EXISTS")
            self.assertEqual(result["a_number"], 1)
            self.assertEqual(read_json(paths.watchlist)["next_w_id"], 1)
            self.assertEqual(read_json(paths.watchlist_w_watermark)["next_w_id"], 1)
            self.assertEqual(read_json(paths.watchlist_control)["request_receipts"][0]["outcome"], "formal_exists")

    def test_invalid_strict_inputs_do_not_write_any_observation_file(self) -> None:
        rejected = (
            "https://douyin.com:8443/user/42",
            "https://user@www.douyin.com/user/42",
            "https://www.douyin.com/user/a/b",
            "https://www.douyin.com/user/a%2Fb",
        )
        for url in rejected:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                paths, service = self._service(directory)
                before = {
                    path: read_json(path)
                    for path in (paths.watchlist, paths.watchlist_w_watermark, paths.watchlist_control)
                }
                with self.assertRaises(WatchlistError) as caught:
                    service.observe(self._payload(url))
                self.assertEqual(caught.exception.code, "INVALID")
                for path, value in before.items():
                    self.assertEqual(read_json(path), value)

    def test_pending_receipt_repairs_only_when_the_reserved_body_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload()
            real_write = watchlist_module.write_json_atomic

            def fail_body(path: Path, value: dict[str, object]) -> None:
                if path.resolve() == paths.watchlist.resolve():
                    raise PermissionError("synthetic body write failure")
                real_write(path, value)

            with patch.object(watchlist_module, "write_json_atomic", side_effect=fail_body):
                with self.assertRaises(PermissionError):
                    service.observe(payload)
            self.assertEqual(read_json(paths.watchlist_w_watermark)["next_w_id"], 2)
            self.assertEqual(read_json(paths.watchlist)["records"], [])
            self.assertTrue(service.has_unresolved_recovery())
            with self.assertRaises(WatchlistError) as same_request:
                service.observe(payload)
            self.assertEqual(same_request.exception.code, "RECOVERY_REQUIRED")
            other = self._payload()
            other["url"] = payload["url"]
            with self.assertRaises(WatchlistError) as same_url:
                service.observe(other)
            self.assertEqual(same_url.exception.code, "RECOVERY_REQUIRED")

    def test_pending_receipt_with_body_is_repaired_without_recomputing_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload()
            service.observe(payload)
            control = read_json(paths.watchlist_control)
            control["request_receipts"][0]["outcome"] = "pending"
            write_json_atomic(paths.watchlist_control, control)
            before = read_json(paths.watchlist)
            replay = service.observe(payload)
            self.assertEqual(replay["status"], "EXISTS")
            self.assertEqual(read_json(paths.watchlist), before)
            self.assertEqual(read_json(paths.watchlist_control)["request_receipts"][0]["outcome"], "committed")

    def test_watermark_never_reuses_a_number_when_document_is_ahead(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            service.observe(self._payload())
            stale = read_json(paths.watchlist_w_watermark)
            stale["next_w_id"] = 1
            write_json_atomic(paths.watchlist_w_watermark, stale)
            second = self._payload()
            second["url"] = "https://www.douyin.com/user/43"
            result = service.observe(second)
            self.assertEqual(result["w_id"], 2)
            self.assertEqual(read_json(paths.watchlist_w_watermark)["next_w_id"], 3)

    def test_archive_delete_and_new_request_preserve_high_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            first = self._payload()
            created = service.observe(first)
            archived = service.archive(created["w_id"], expected_revision=1)
            duplicate = self._payload(first["url"])
            duplicate_result = service.observe(duplicate)
            self.assertEqual(duplicate_result["status"], "ARCHIVED")
            self.assertEqual(duplicate_result["w_id"], 1)
            deleted_request_id = str(uuid.uuid4())
            deleted = service.delete_archived(
                1,
                request_id=deleted_request_id,
                expected_revision=archived.revision,
            )
            replayed_delete = service.delete_archived(
                1,
                request_id=deleted_request_id,
                expected_revision=deleted.revision,
            )
            self.assertEqual(replayed_delete.revision, deleted.revision)
            self.assertEqual(read_json(paths.watchlist)["records"], [])
            fresh = self._payload(first["url"])
            fresh_result = service.observe(fresh)
            self.assertEqual(fresh_result["w_id"], 2)
            self.assertEqual(read_json(paths.watchlist_w_watermark)["next_w_id"], 3)
            validate_control(read_json(paths.watchlist_control))

    def test_delete_pending_replay_repairs_missing_body_without_repeating_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            created = service.observe(self._payload())
            service.archive(created["w_id"], expected_revision=1)
            request_id = str(uuid.uuid4())
            real_write = watchlist_module.write_json_atomic
            control_writes = 0

            def fail_final_control(path: Path, value: dict[str, object]) -> None:
                nonlocal control_writes
                if path.resolve() == paths.watchlist_control.resolve():
                    control_writes += 1
                    if control_writes == 2:
                        raise PermissionError("synthetic final delete receipt failure")
                real_write(path, value)

            with patch.object(watchlist_module, "write_json_atomic", side_effect=fail_final_control):
                with self.assertRaises(PermissionError):
                    service.delete_archived(1, request_id=request_id, expected_revision=2)
            self.assertEqual(read_json(paths.watchlist)["records"], [])
            self.assertEqual(
                read_json(paths.watchlist_control)["request_receipts"][-1]["outcome"],
                "delete_pending",
            )
            repaired = service.delete_archived(1, request_id=request_id, expected_revision=3)
            self.assertEqual(repaired.revision, 3)
            self.assertEqual(
                read_json(paths.watchlist_control)["request_receipts"][-1]["outcome"],
                "deleted",
            )

    def test_missing_committed_body_blocks_replay_and_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload()
            service.observe(payload)
            document = read_json(paths.watchlist)
            document["records"] = []
            write_json_atomic(paths.watchlist, document)
            self.assertTrue(service.has_unresolved_recovery())
            with self.assertRaises(WatchlistError) as replay:
                service.observe(payload)
            self.assertEqual(replay.exception.code, "RECOVERY_REQUIRED")
            with self.assertRaises(WatchlistError) as ready:
                service.mark_ready()
            self.assertEqual(ready.exception.code, "RECOVERY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
