"""Acceptance tests for the calculator task; kept outside the writable workspace."""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path


WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE", Path(__file__).resolve().parents[1] / "workspace"))
sys.path.insert(0, str(WORKSPACE))


class CalculatorAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.calculator = importlib.import_module("calculator")

    def test_add(self) -> None:
        self.assertEqual(self.calculator.add(2, 3), 5)

    def test_subtract(self) -> None:
        self.assertEqual(self.calculator.subtract(10, 3), 7)

    def test_multiply(self) -> None:
        self.assertEqual(self.calculator.multiply(4, 5), 20)

    def test_divide(self) -> None:
        self.assertEqual(self.calculator.divide(10, 2), 5)

    def test_float_inputs(self) -> None:
        self.assertEqual(self.calculator.add(1.5, 2.5), 4.0)

    def test_divide_by_zero_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            self.calculator.divide(10, 0)


if __name__ == "__main__":
    unittest.main()
