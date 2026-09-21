"""Small offline checks for the load probe's measurement and safety logic."""

import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "load_dds.py"
SPEC = importlib.util.spec_from_file_location("load_dds", SCRIPT)
load_dds = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(load_dds)


class PercentileTests(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(load_dds.percentile([], 95))

    def test_nearest_rank_p95(self):
        self.assertEqual(load_dds.percentile(list(range(1, 101)), 95), 95)

    def test_single_sample(self):
        self.assertEqual(load_dds.percentile([12.5], 95), 12.5)


if __name__ == "__main__":
    unittest.main()
