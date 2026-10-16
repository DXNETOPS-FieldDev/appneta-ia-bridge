"""Regression coverage for `python -m alarm_bridge` itself.

Guards two failure modes that tests calling alarm_bridge.cli.main()
directly would not catch, because they never exercise the module entrypoint:
  1. __main__.py lived outside the alarm_bridge package, so `python -m
     alarm_bridge` failed immediately with "No module named
     alarm_bridge.__main__" -- broke both Docker and local usage.
  2. __main__.py called main() without propagating its return value via
     sys.exit(), so the process always exited 0 even when config loading
     failed and an error was printed to stderr.
"""

import subprocess
import sys
import unittest


class TestModuleEntrypoint(unittest.TestCase):
    def test_missing_config_exits_nonzero_with_clear_error(self):
        result = subprocess.run(
            [sys.executable, "-m", "alarm_bridge"],
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},  # deliberately no TRAP_DESTINATION_1_HOST
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TRAP_DESTINATION_1_HOST", result.stderr)


if __name__ == "__main__":
    unittest.main()
