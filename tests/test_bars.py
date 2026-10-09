"""Pure display geometry tests with no terminal or hardware requirements."""

import unittest

from musatop.bars import area_rows, bar_cells, percent_label, spark_cells, trend_values
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
            HistoryPoint(710, 80, 10),  # absolute bucket 68 at this width
            HistoryPoint(711, 30, None),
            HistoryPoint(1000, 100, 0),
            HistoryPoint(1001, 99, 99),  # future samples cannot leak in
        ]
        util = trend_values(points, "util_percent", 30, 1000)
        self.assertEqual(util[:2], [10, 80])
        self.assertEqual(util[-1], 100)
        self.assertEqual(util[2:29], [None] * 27)
        memory = trend_values(points, "memory_percent", 30, 1000)
        self.assertEqual(memory[0], 20)
        self.assertEqual(memory[1], 10)
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

    def test_closed_peaks_only_translate_without_changing_shape(self):
        points = [HistoryPoint(995, 81, None), HistoryPoint(997, 89, None)]
        for width in (10, 30, 66, 70, 134, 300, 301, 601):
            def silhouette(now):
                result = trend_values(points, "util_percent", width, now)
                occupied = [(i, value) for i, value in enumerate(result) if value is not None]
                first = occupied[0][0]
                return [(i - first, value) for i, value in occupied]

            expected = silhouette(1010)
            for now in range(1011, 1150):
                with self.subTest(width=width, now=now):
                    self.assertEqual(silhouette(now), expected)
        # This pair used to repeatedly merge and separate in a moving window.
        self.assertEqual(
            [value for value in trend_values(points, "util_percent", 66, 1100)
             if value is not None],
            [81, 89],
        )

    def test_open_bucket_updates_peak_then_keeps_it_when_closed(self):
        points = [HistoryPoint(997, 10, None)]
        self.assertEqual(trend_values(points, "util_percent", 66, 1000)[-1], 10)
        points.extend([HistoryPoint(1000, 75, None), HistoryPoint(1001, 60, None)])
        self.assertEqual(trend_values(points, "util_percent", 66, 1001)[-1], 75)
        self.assertEqual(trend_values(points, "util_percent", 66, 1002)[-2:], [75, None])

    def test_only_expired_samples_leave_oldest_partial_bucket(self):
        points = [HistoryPoint(701, 90, None), HistoryPoint(702, 10, None)]
        self.assertEqual(trend_values(points, "util_percent", 30, 1000)[0], 90)
        self.assertEqual(trend_values(points, "util_percent", 30, 1001)[0], 10)
        self.assertEqual(trend_values(points, "util_percent", 30, 1002), [None] * 30)

    def test_irregular_samples_and_missing_buckets_are_not_interpolated(self):
        points = [HistoryPoint(second, 50, None) for second in (800, 810, 815, 880, 990)]
        values = trend_values(points, "util_percent", 301, 1000)
        self.assertEqual(sum(value is not None for value in values), len(points))
        self.assertTrue(all(value is None for value in values[116:180]))
        wide = trend_values(points, "util_percent", 601, 1000)
        self.assertEqual(sum(value is not None for value in wide), len(points))
        self.assertTrue(all(wide[i] is None for i in range(1, 601, 2)))

    def test_resize_reprojects_without_mutating_history(self):
        points = [HistoryPoint(995, 81, None), HistoryPoint(997, 89, None)]
        original = points[:]
        narrow = trend_values(points, "util_percent", 10, 1000)
        wide = trend_values(points, "util_percent", 301, 1000)
        self.assertEqual(sum(value is not None for value in narrow), 1)
        self.assertEqual(sum(value is not None for value in wide), 2)
        self.assertEqual(points, original)

    def test_invalid_samples_stay_unknown(self):
        points = [HistoryPoint(1000, value, None) for value in (None, -1, 101, float("nan"))]
        self.assertEqual(trend_values(points, "util_percent", 70, 1000), [None] * 70)


class AreaTests(unittest.TestCase):
    @staticmethod
    def decode(rows):
        """Decode independent Braille geometry into a grid of boolean dots."""
        grid = []
        for row in rows:
            for masks in ((1, 8), (2, 16), (4, 32), (64, 128)):
                dots = []
                for cell in row:
                    bits = 0 if cell == " " else ord(cell) - 0x2800
                    dots.extend(bool(bits & mask) for mask in masks)
                grid.append(dots)
        return grid

    def test_fixed_height_distinguishes_69_99_and_full(self):
        grid = self.decode(area_rows([0, 69, 99, 100], 3))
        self.assertEqual([sum(row[x] for row in grid) for x in range(4)], [1, 8, 11, 12])
        self.assertTrue(all(grid[-1]))

    def test_missing_is_blank_and_observed_zero_has_baseline(self):
        self.assertEqual(area_rows([None, None], 3), [" ", " ", " "])
        self.assertEqual(area_rows([0, None], 3), [" ", " ", "⡀"])
        self.assertEqual(area_rows([None, 0], 3), [" ", " ", "⢀"])
        for value in (float("nan"), float("inf"), -1, 101):
            self.assertEqual(area_rows([value, value], 2), [" ", " "])

    def test_mirror_reverses_dot_geometry_including_partial_cells(self):
        values = [None, 0, 13, 30, 69, 99, 100]
        upright = self.decode(area_rows(values, 3))
        mirrored = self.decode(area_rows(values, 3, upside_down=True))
        self.assertEqual(mirrored, list(reversed(upright)))
        self.assertEqual(area_rows([0, None], 2, upside_down=True), ["⠁", " "])

    def test_dimensions_and_odd_sample_padding(self):
        self.assertEqual(area_rows([100], 2), ["⡇", "⡇"])
        self.assertEqual(area_rows([], 2), ["", ""])
        for height in (0, 1, 2, 5):
            for count in (0, 1, 2, 9):
                rows = area_rows([50] * count, height)
                self.assertEqual(len(rows), height)
                self.assertTrue(all(len(row) == (count + 1) // 2 for row in rows))
        self.assertEqual(area_rows([100], -1), [])

    def test_ascii_geometry_has_partial_cells_and_mirrors(self):
        values = [None, 0, 69, 99, 100]
        rows = area_rows(values, 3, ascii=True)
        self.assertEqual(rows, ["   :#", "  ###", " :###"])
        self.assertEqual(area_rows(values, 3, upside_down=True, ascii=True), rows[::-1])
        self.assertTrue(all(row.isascii() for row in rows))


if __name__ == "__main__":
    unittest.main()
