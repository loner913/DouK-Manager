from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTableView, QPushButton

from douk_manager.core.watchlist import WatchlistSnapshot
from douk_manager.gui import WatchlistTableModel


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


if __name__ == "__main__":
    unittest.main()
