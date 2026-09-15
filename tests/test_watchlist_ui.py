from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt, QDate, QTime
from datetime import datetime, timezone
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QPushButton,
    QTableView,
    QTextEdit,
)

from douk_manager.core.watchlist import WatchlistSnapshot
from douk_manager.core.watchlist_promotion import PromotionResult
from douk_manager.gui import MainWindow, WatchlistTableModel, WatchlistReviewDialog
from douk_manager.startup import StartupState


def _record(
    w_id: int,
    *,
    state: str = "watching",
    next_review_at: str | None = "2020-01-01T00:00:00Z",
) -> dict[str, object]:
    return {
        "w_id": w_id,
        "url": f"https://www.douyin.com/user/synthetic-ui-{w_id}",
        "display_name": f"合成观察名称 {w_id}",
        "captured_nickname": f"合成昵称 {w_id}",
        "douyin_id": f"synthetic_ui_{w_id}",
        "nickname_blank": False,
        "state": state,
        "reasons": ["few_works"],
        "note": f"合成备注 {w_id}",
        "created_at": "2026-09-13T00:00:00Z",
        "updated_at": "2026-09-13T00:00:00Z",
        "next_review_at": next_review_at,
        "review_history": [],
        "archived_at": None,
        "promoted_a_number": None,
        "promotion_recovery": None,
    }


class WatchlistUiModelTests(unittest.TestCase):
    def test_review_local_date_and_original_timestamp(self):
        record = _record(1, next_review_at="2099-01-01T03:04:05Z")
        dialog = WatchlistReviewDialog(record)
        self.assertEqual(dialog._selected_review_time(), record["next_review_at"])
        self.assertEqual(dialog.save_button.text(), "保存复查")
        self.assertEqual(dialog.cancel_button.property("legacyRole"), "secondary")
        self.assertEqual(
            dialog.review_time.buttonSymbols(),
            QAbstractSpinBox.ButtonSymbols.NoButtons,
        )
        self.assertFalse(dialog.review_time.lineEdit().isReadOnly())
        self.assertNotEqual(dialog.review_mode.findData(15), -1)
        self.assertNotEqual(dialog.review_mode.findData(45), -1)
        dialog.review_mode.setCurrentIndex(dialog.review_mode.findData("custom"))
        dialog.review_date.setDate(QDate(2099, 2, 3))
        dialog.review_time.setTime(QTime(14, 35))
        expected = datetime(2099, 2, 3, 14, 35).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        dialog._accept()
        self.assertEqual(dialog.result[2], expected)

    def test_review_dialog_name_cancel_and_accept(self):
        record = _record(1)
        dialog = WatchlistReviewDialog(record)
        self.assertEqual(dialog.display_name_edit.text(), record["display_name"])
        dialog.display_name_edit.setText("Edited name")
        dialog.reject()
        self.assertIsNone(dialog.result)
        self.assertNotEqual(record["display_name"], "Edited name")
        dialog = WatchlistReviewDialog(record)
        dialog.display_name_edit.setText("Edited name")
        dialog._accept()
        self.assertEqual(dialog.result[3], "Edited name")
        self.assertNotEqual(record["display_name"], "Edited name")

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_model_view_has_no_full_url_or_per_row_widgets(self) -> None:
        model = WatchlistTableModel()
        model.set_snapshot(
            WatchlistSnapshot(
                revision=7,
                next_w_id=2,
                records=(_record(1),),
            )
        )
        table = QTableView()
        table.setModel(model)

        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.columnCount(), 9)
        visible_text = " ".join(
            str(model.data(model.index(0, column), Qt.ItemDataRole.DisplayRole))
            for column in range(model.columnCount())
        )
        self.assertNotIn("https://", visible_text)
        self.assertNotIn("douyin.com", visible_text)
        self.assertEqual(table.findChildren(QPushButton), [])

    def test_state_due_and_search_filters_keep_model_rows_only(self) -> None:
        model = WatchlistTableModel()
        model.set_snapshot(
            WatchlistSnapshot(
                revision=8,
                next_w_id=4,
                records=(
                    _record(1),
                    _record(2, next_review_at="2099-01-01T00:00:00Z"),
                    _record(3, state="archived", next_review_at=None),
                ),
            )
        )

        self.assertEqual(model.total_count, 3)
        self.assertEqual(model.visible_count, 3)
        model.set_filters("watching", "due", "")
        self.assertEqual(model.visible_count, 1)
        self.assertEqual(model.record_at(0)["w_id"], 1)

        model.set_filters("all", "all", "synthetic_ui_2")
        self.assertEqual(model.visible_count, 1)
        self.assertEqual(model.record_at(0)["w_id"], 2)


class WatchlistPromotionFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.output = QTextEdit()
        self.button = QPushButton()
        self.pending = []
        self.window = SimpleNamespace(
            controller=SimpleNamespace(startup_state=StartupState.READY),
            watchlist_output=self.output,
            watchlist_refresh_button=self.button,
            watchlist_model=WatchlistTableModel(),
            _watchlist_promotion_preview=object(),
            _watchlist_snapshot=None,
            _selected_watchlist_record=Mock(return_value=None),
            watchlist_table=QTableView(),
            _refresh_watchlist_reminders=Mock(),
            _apply_watchlist_filters=Mock(),
            _update_watchlist_button_states=Mock(),
            _append_info=MainWindow._append_info,
            _replace_info=MainWindow._replace_info,
            statusBar=Mock(return_value=Mock()),
        )
        for name in (
            "refresh_watchlist", "_apply_watchlist_snapshot",
            "_watchlist_background_failed", "_watchlist_promotion_succeeded",
            "_watchlist_promotion_failed",
        ):
            setattr(self.window, name, getattr(MainWindow, name).__get__(self.window))

        def submit(spec, action, **callbacks):
            self.pending.append(callbacks)
            return "synthetic-refresh"

        self.window._submit_coalesced_background = submit
        record = _record(1, state="promoted")
        record["promoted_a_number"] = 1
        self.snapshot = WatchlistSnapshot(revision=4, next_w_id=2, records=(record,))

    def tearDown(self) -> None:
        self.output.close()
        self.button.close()

    def test_promotion_success_survives_deferred_refresh_and_updates_rows(self) -> None:
        self.window._watchlist_promotion_succeeded(
            PromotionResult(1, 1, 4, "available", False)
        )
        self.assertIn("W1 已完成转正：A1", self.output.toPlainText())
        self.pending.pop()["on_success"](self.snapshot)
        text = self.output.toPlainText()
        self.assertIn("W1 已完成转正：A1", text)
        self.assertIn("观察名单已刷新", text)
        self.assertEqual(self.window.watchlist_model.record_at(0)["state"], "promoted")
        self.assertIsNone(self.window._watchlist_promotion_preview)

    def test_promotion_failure_survives_deferred_refresh(self) -> None:
        self.window._watchlist_promotion_failed("synthetic transaction failure")
        self.pending.pop()["on_success"](self.snapshot)
        self.assertIn("synthetic transaction failure", self.output.toPlainText())
        self.assertIn("观察名单已刷新", self.output.toPlainText())

    def test_refresh_failure_keeps_success_and_reports_refresh_error(self) -> None:
        self.window._watchlist_promotion_succeeded(
            PromotionResult(1, 1, 4, "available", False)
        )
        self.pending.pop()["on_failure"]("synthetic refresh failure")
        self.assertIn("W1 已完成转正：A1", self.output.toPlainText())
        self.assertIn("synthetic refresh failure", self.output.toPlainText())

    def test_manual_refresh_replaces_previous_operation_output(self) -> None:
        self.output.setPlainText("previous operation")
        self.window.refresh_watchlist()
        self.pending.pop()["on_success"](self.snapshot)
        self.assertNotIn("previous operation", self.output.toPlainText())
        self.assertIn("观察名单已刷新", self.output.toPlainText())


if __name__ == "__main__":
    unittest.main()
