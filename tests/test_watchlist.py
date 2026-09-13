from __future__ import annotations

import copy
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
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

    def test_pending_receipt_recovers_original_reserved_body_only(self) -> None:
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
            other = self._payload()
            other["url"] = payload["url"]
            with self.assertRaises(WatchlistError) as same_url:
                service.observe(other)
            self.assertEqual(same_url.exception.code, "RECOVERY_REQUIRED")
            result = service.observe(payload)
            self.assertEqual(result["w_id"], 1)
            self.assertEqual(service.snapshot().next_w_id, 2)
            self.assertEqual(len(service.snapshot().records), 1)
            self.assertFalse(service.has_unresolved_recovery())

    def _leave_missing_pending(self, service, payload):
        real_write = watchlist_module.write_json_atomic

        def fail_body(path, value):
            if path.resolve() == service.paths.watchlist.resolve():
                raise PermissionError("synthetic body failure")
            real_write(path, value)

        with patch.object(watchlist_module, "write_json_atomic", side_effect=fail_body):
            with self.assertRaises(PermissionError):
                service.observe(payload)

    def test_pending_recovery_preserves_expired_reminder_and_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload()
            payload["next_review_at"] = "2026-01-02T00:00:00Z"
            payload.pop("review_after_days", None)

            class BeforeReminder(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime(2026, 1, 1, tzinfo=timezone.utc)

            with patch.object(watchlist_module, "datetime", BeforeReminder):
                self._leave_missing_pending(service, payload)
            with patch.object(service, "_resolve_review_time", side_effect=AssertionError("must reuse persisted time")):
                self.assertEqual(service.observe(payload)["w_id"], 1)
                before = read_json(paths.watchlist)
                self.assertEqual(service.observe(payload)["w_id"], 1)
            self.assertEqual(read_json(paths.watchlist), before)
            self.assertEqual(before["records"][0]["next_review_at"], payload["next_review_at"])

    def test_pending_recovery_rejects_conflicting_payload_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload()
            self._leave_missing_pending(service, payload)
            files = (paths.watchlist, paths.watchlist_w_watermark, paths.watchlist_control)
            before = [p.read_bytes() for p in files]
            for altered in (dict(payload, note="changed"), dict(payload, request_id=str(uuid.uuid4()))):
                with self.assertRaises(WatchlistError):
                    service.observe(altered)
                self.assertEqual([p.read_bytes() for p in files], before)

    def test_pending_recovery_repeated_body_and_receipt_failures_are_retryable(self) -> None:
        for target in ("body", "receipt"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                paths, service = self._service(directory)
                payload = self._payload()
                self._leave_missing_pending(service, payload)
                real_write = watchlist_module.write_json_atomic

                def fail_write(path, value):
                    failed = paths.watchlist if target == "body" else paths.watchlist_control
                    if path.resolve() == failed.resolve():
                        raise PermissionError("synthetic recovery write failure")
                    real_write(path, value)

                with patch.object(watchlist_module, "write_json_atomic", side_effect=fail_write):
                    with self.assertRaises(PermissionError):
                        service.observe(payload)
                self.assertEqual(service.observe(payload)["w_id"], 1)
                self.assertEqual(len(service.snapshot().records), 1)
                self.assertEqual(read_json(paths.watchlist_w_watermark)["next_w_id"], 2)

    def test_pending_recovery_keeps_blocked_gate_and_unproven_gaps_closed(self) -> None:
        for case in ("blocked", "missing_receipt", "extra_gap", "formal_exists", "other_pending"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                paths, service = self._service(directory)
                payload = self._payload()
                self._leave_missing_pending(service, payload)
                control = read_json(paths.watchlist_control)
                if case == "blocked":
                    control["write_gate"] = "blocked"
                elif case == "missing_receipt":
                    control["request_receipts"] = []
                elif case == "other_pending":
                    other = copy.deepcopy(control["request_receipts"][0])
                    other["request_id"] = str(uuid.uuid4())
                    control["request_receipts"].append(other)
                elif case == "extra_gap":
                    watermark = read_json(paths.watchlist_w_watermark)
                    watermark["next_w_id"] = 3
                    write_json_atomic(paths.watchlist_w_watermark, watermark)
                elif case == "formal_exists":
                    master = read_json(paths.master_settings)
                    master["accounts_urls"][0]["url"] = payload["url"]
                    write_json_atomic(paths.master_settings, master)
                write_json_atomic(paths.watchlist_control, control)
                files = (paths.watchlist, paths.watchlist_w_watermark, paths.watchlist_control)
                before = [p.read_bytes() for p in files]
                with self.assertRaises(WatchlistError):
                    service.observe(payload)
                self.assertEqual([p.read_bytes() for p in files], before)

    def test_pending_existing_body_must_match_original_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload()
            service.observe(payload)
            control = read_json(paths.watchlist_control)
            control["request_receipts"][0]["outcome"] = "pending"
            write_json_atomic(paths.watchlist_control, control)
            document = read_json(paths.watchlist)
            document["records"][0]["note"] = "different body with same URL"
            write_json_atomic(paths.watchlist, document)
            with self.assertRaises(WatchlistError) as caught:
                service.observe(payload)
            self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
            self.assertEqual(read_json(paths.watchlist_control), control)

    def test_committed_receipt_missing_body_is_not_reconstructed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload()
            service.observe(payload)
            document = read_json(paths.watchlist)
            document["records"] = []
            write_json_atomic(paths.watchlist, document)
            with self.assertRaises(WatchlistError) as caught:
                service.observe(payload)
            self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
            self.assertEqual(read_json(paths.watchlist), document)

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

    def test_deleted_observation_aliases_replay_without_writes(self) -> None:
        for legacy in (False, True):
            with self.subTest(legacy=legacy), tempfile.TemporaryDirectory() as directory:
                paths, service = self._service(directory)
                original = self._payload()
                alias = self._payload(original["url"])
                service.observe(original)
                service.observe(alias)
                archived = service.archive(1, expected_revision=service.snapshot().revision)
                delete_id = str(uuid.uuid4())
                service.delete_archived(1, request_id=delete_id, expected_revision=archived.revision)
                control = read_json(paths.watchlist_control)
                self.assertTrue(all(item["outcome"] == "deleted" for item in control["request_receipts"]))
                if legacy:
                    control["request_receipts"][0]["outcome"] = "committed"
                    control["request_receipts"][1]["outcome"] = "exists"
                    write_json_atomic(paths.watchlist_control, control)
                targets = (paths.watchlist, paths.watchlist_control, paths.watchlist_w_watermark)
                before = [p.read_bytes() for p in targets]
                for payload in (original, alias):
                    result = service.observe(payload)
                    self.assertEqual((result["status"], result["w_id"]), ("DELETED", 1))
                    with self.assertRaises(WatchlistError) as raised:
                        service.observe({**payload, "note": "different digest"})
                    self.assertEqual(raised.exception.code, "REQUEST_CONFLICT")
                with self.assertRaises(WatchlistError) as raised:
                    service.delete_archived(2, request_id=delete_id, expected_revision=3)
                self.assertEqual(raised.exception.code, "REQUEST_CONFLICT")
                self.assertEqual(before, [p.read_bytes() for p in targets])
                fresh = service.observe(self._payload(original["url"]))
                self.assertEqual(fresh["w_id"], 2)
                self.assertEqual(service.observe(original)["status"], "DELETED")

    def test_interrupted_delete_blocks_original_request_before_body_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload()
            service.observe(payload)
            archived = service.archive(1, expected_revision=1)
            real_write = watchlist_module.write_json_atomic

            def fail_body(path, value):
                if path == paths.watchlist:
                    raise PermissionError("synthetic body deletion failure")
                real_write(path, value)

            with patch.object(watchlist_module, "write_json_atomic", side_effect=fail_body):
                with self.assertRaises(PermissionError):
                    service.delete_archived(1, request_id=str(uuid.uuid4()), expected_revision=archived.revision)
            self.assertEqual(service.snapshot().records[0]["state"], "archived")
            with self.assertRaises(WatchlistError) as raised:
                service.observe(payload)
            self.assertEqual(raised.exception.code, "RECOVERY_REQUIRED")

    def test_delete_pending_replay_repairs_missing_body_without_repeating_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service = self._service(directory)
            payload = self._payload()
            created = service.observe(payload)
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
            with self.assertRaises(WatchlistError) as raised:
                service.observe(payload)
            self.assertEqual(raised.exception.code, "RECOVERY_REQUIRED")
            repaired = service.delete_archived(1, request_id=request_id, expected_revision=3)
            self.assertEqual(service.observe(payload)["status"], "DELETED")
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
