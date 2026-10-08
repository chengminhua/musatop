"""Pure display geometry tests with no terminal or hardware requirements."""

import unittest

from musatop.bars import bar_cells, percent_label, spark_cells, trend_values
from musatop.history import HistoryPoint


class BarTests(unittest.TestCase):
    def test_empty_full_and_eighth_cell_fill(self):
        self.assertEqual(bar_cells(0, 8), " " * 8)
        self.assertEqual(bar_cells(100, 8), "█" * 8)
        self.assertEqual(bar_cells(6.25, 8), "▌" + " " * 7)
        self.assertEqual(bar_cells(18.75, 8), "█▌" + " " * 6)
        self.assertEqual(bar_cells(99, 8), "█" * 7 + "▉")

    def test_unknown_and_invalid_are_not_zero(self):
        for value in (None, float("nan"), float("inf"), -1, 101):
            with self.subTest(value=value):
                self.assertEqual(bar_cells(value, 8), "░" * 8)
                self.assertEqual(bar_cells(value, 8, ascii=True), "?" * 8)
                self.assertEqual(percent_label(value), " N/A")

    def test_width_and_percent_positions_are_constant(self):
        for width in (0, 1, 5, 20, 100):
            for value in (None, 0, 0.1, 3, 25, 99.9, 100):
                for ascii in (True, False):
                    with self.subTest(width=width, value=value, ascii=ascii):
                        self.assertEqual(len(bar_cells(value, width, ascii=ascii)), width)
                        self.assertEqual(len(percent_label(value)), 4)

    def test_ascii_bar_has_no_non_ascii_characters(self):
        self.assertEqual(bar_cells(25, 8, ascii=True), "##      ")
        self.assertEqual(bar_cells(100, 8, ascii=True), "########")
        self.assertEqual(bar_cells(0, 8, ascii=True), "        ")


class TrendTests(unittest.TestCase):
    def test_window_edges_and_peak_projection(self):
        points = [
            HistoryPoint(700, 99, 99),  # just outside the window
            HistoryPoint(701, 10, 20),
            HistoryPoint(710, 80, 10),  # same first ten-second display column
            HistoryPoint(711, 30, None),
            HistoryPoint(1000, 100, 0),
            HistoryPoint(1001, 99, 99),  # future samples cannot leak in
        ]
        util = trend_values(points, "util_percent", 30, 1000)
        self.assertEqual(util[:2], [80, 30])
        self.assertEqual(util[-1], 100)
        self.assertEqual(util[2:29], [None] * 27)
        memory = trend_values(points, "memory_percent", 30, 1000)
        self.assertEqual(memory[0], 20)
        self.assertIsNone(memory[1])
        self.assertEqual(memory[-1], 0)

    def test_missing_seconds_stay_blank_and_zero_is_visible(self):
        points = [HistoryPoint(1000, 0, None)]
        values = trend_values(points, "util_percent", 60, 1000)
        self.assertEqual(values[:-1], [None] * 59)
        self.assertEqual(spark_cells(values), " " * 59 + "▁")
        self.assertEqual(spark_cells(values, ascii=True), " " * 59 + ".")
        self.assertEqual(spark_cells([None, 0, 50, 100]), " ▁▅█")
        self.assertEqual(spark_cells([None, 0, 50, 100], ascii=True), " .=@")

    def test_single_column_uses_peak_and_fixed_scale(self):
        points = [HistoryPoint(701, 10, 20), HistoryPoint(1000, 25, 15)]
        self.assertEqual(trend_values(points, "util_percent", 1, 1000), [25])
        self.assertEqual(spark_cells([25]), "▃")
        self.assertEqual(spark_cells([25, 75]), "▃▇")
        self.assertEqual(trend_values(points, "util_percent", 0, 1000), [])
        with self.assertRaises(ValueError):
            trend_values(points, "unknown", 10, 1000)


if __name__ == "__main__":
    unittest.main()
