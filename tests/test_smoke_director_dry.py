"""The manual HTTP smoke must also finish without a server or credentials."""
from pathlib import Path
import subprocess
import sys
import unittest


class SmokeDirectorDryTests(unittest.TestCase):
    def test_all_steps_dry(self):
        script = Path(__file__).resolve().parents[1] / "tools" / "smoke_director.py"
        result = subprocess.run(
            [sys.executable, str(script), "--dry", "--server", "http://invalid.invalid:1"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for step in ("inpaint", "generate", "final"):
            self.assertIn("PASS " + step, result.stdout)
        self.assertIn('"SEA WATCH 1887"', result.stdout)
        self.assertIn("TOTAL actual_cost=", result.stdout)
        self.assertIn("day_cost=", result.stdout)


if __name__ == "__main__":
    unittest.main()
