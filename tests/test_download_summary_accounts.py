import unittest

from douk_manager.core.download_summary import SummaryInputError, freeze_planned_accounts


class PlannedAccountTests(unittest.TestCase):
    def test_only_enabled_nonempty_urls_form_task_sequence(self) -> None:
        document = {
            "accounts_urls": [
                {"mark": "A1one", "url": "https://example/1", "enable": False},
                {"mark": "A2two", "url": "https://example/2", "enable": True},
                {"mark": "A3three", "url": "", "enable": True},
                {"mark": "A4four", "url": "https://example/4", "enable": True},
            ]
        }

        result = freeze_planned_accounts(document)

        self.assertEqual(
            [(item.task_index, item.a_number, item.mark) for item in result],
            [(1, 2, "A2two"), (2, 4, "A4four")],
        )

    def test_a1_does_not_accept_a10_mark(self) -> None:
        with self.assertRaisesRegex(SummaryInputError, "A1"):
            freeze_planned_accounts(
                {"accounts_urls": [{"mark": "A10wrong", "url": "https://example/1"}]}
            )
