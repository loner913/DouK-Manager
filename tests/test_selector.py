from __future__ import annotations

import unittest

from douk_manager.core.selector import SelectionError, compact_numbers, parse_selection, split_batches


class SelectorTests(unittest.TestCase):
    def test_mixed_a_expression(self) -> None:
        result = parse_selection("A1,A10-A99,A879", 1000)
        self.assertEqual(result.count, 92)
        self.assertEqual(result.numbers[0], 1)
        self.assertEqual(result.numbers[-1], 879)

    def test_chinese_separator_and_duplicates(self) -> None:
        result = parse_selection("A1，1，A2-A4、A4", 10)
        self.assertEqual(result.numbers, (1, 2, 3, 4))
        self.assertEqual(result.duplicate_numbers, (1, 4))

    def test_out_of_range_is_blocked(self) -> None:
        with self.assertRaises(SelectionError):
            parse_selection("A1,A11", 10)

    def test_descending_range_is_blocked(self) -> None:
        with self.assertRaises(SelectionError):
            parse_selection("A9-A2", 10)

    def test_compact_and_batches(self) -> None:
        self.assertEqual(compact_numbers([1, 2, 3, 8, 10, 11]), "A1-A3,A8,A10-A11")
        self.assertEqual(
            split_batches(1, 1392, 250),
            ((1, 250), (251, 500), (501, 750), (751, 1000), (1001, 1250), (1251, 1392)),
        )


if __name__ == "__main__":
    unittest.main()

