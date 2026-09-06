from __future__ import annotations

import unittest

from cursor_usage_monitor.core import bucket_of, money_bucket, percent_bucket


class BucketTests(unittest.TestCase):
    def test_percent_steps(self) -> None:
        self.assertEqual(bucket_of(0, 5), 0)
        self.assertEqual(bucket_of(4.9, 5), 0)
        self.assertEqual(bucket_of(5, 5), 5)
        self.assertEqual(bucket_of(5.1, 5), 5)
        self.assertEqual(bucket_of(9.99, 5), 5)
        self.assertEqual(bucket_of(10, 5), 10)
        self.assertEqual(bucket_of(99.9, 5), 95)
        self.assertEqual(percent_bucket(100), 100)
        self.assertEqual(percent_bucket(104), 100)

    def test_on_demand_ten_dollar_steps(self) -> None:
        self.assertEqual(money_bucket(0), 0)
        self.assertEqual(money_bucket(9.99), 0)
        self.assertEqual(money_bucket(10), 10)
        self.assertEqual(money_bucket(19.99), 10)
        self.assertEqual(money_bucket(20), 20)
        self.assertEqual(bucket_of(10, 10), 10)

    def test_negative_clamps(self) -> None:
        self.assertEqual(bucket_of(-3, 5), 0)

    def test_bad_step(self) -> None:
        with self.assertRaises(ValueError):
            bucket_of(10, 0)


if __name__ == "__main__":
    unittest.main()
