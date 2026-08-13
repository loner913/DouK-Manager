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

    def test_numeric_leading_mark_is_frozen_by_array_position(self) -> None:
        result = freeze_planned_accounts(
            {
                "accounts_urls": [
                    *({"mark": "", "url": "", "enable": False} for _ in range(115)),
                    {"mark": "A1164example", "url": "https://example/116"},
                ]
            }
        )

        self.assertEqual(result, (type(result[0])(1, 116, "A1164example"),))

    def test_only_nonempty_string_urls_are_eligible(self) -> None:
        document = {
            "accounts_urls": [
                {"mark": "A1", "url": None},
                {"mark": "A2", "url": 2},
                {"mark": "A3", "url": True},
                {"mark": "A4", "url": {}},
                {"mark": "A5", "url": []},
                {"mark": "A6", "url": "  https://example/6  "},
            ]
        }

        result = freeze_planned_accounts(document)

        self.assertEqual([(item.task_index, item.a_number) for item in result], [(1, 6)])
