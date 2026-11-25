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

import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh"


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

    def test_missing_source_address_exits_nonzero_naming_variable(self):
        """Everything else valid, SOURCE_ADDRESS unset: the only failure is the claim."""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "alarm_bridge"],
                capture_output=True,
                text=True,
                timeout=10,
                env={
                    "PATH": "/usr/bin:/bin",
                    "TRAP_DESTINATION_1_HOST": "192.0.2.10",
                    "TRAP_DESTINATION_1_SCHEME": "general",
                    "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
                },
            )
        except subprocess.TimeoutExpired:
            self.fail("bridge did not exit within 10s; SOURCE_ADDRESS validation regressed")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TRAP_DESTINATION_1_SOURCE_ADDRESS", result.stderr)


class TestEntrypointTlsCheck(unittest.TestCase):
    """The TLS pre-flight in docker/entrypoint.sh names what is wrong and shows the modes and owners.

    Runs a copy of the real script with the hard-coded TLS directory pointed at a temp directory. Only
    the failure paths are run: success would start python and nginx.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.tls = self.tmp / "tls"
        text = ENTRYPOINT.read_text(encoding="utf-8")
        patched, count = re.subn(r"^TLS_DIR=.*$", f"TLS_DIR={self.tls}", text, flags=re.M)
        self.assertEqual(count, 1, "entrypoint.sh no longer has a TLS_DIR= line")
        self.script = self.tmp / "entrypoint.sh"
        self.script.write_text(patched, encoding="utf-8")

    def run_script(self):
        result = subprocess.run(
            ["bash", str(self.script)], capture_output=True, text=True, timeout=10,
            env={"PATH": "/usr/bin:/bin", "TLS_ENABLED": "true"},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        return result.stderr

    def test_missing_directory_is_named_as_not_mounted(self):
        err = self.run_script()
        self.assertIn("does not exist", err)
        self.assertIn("no certificate directory is mounted", err)

    def test_missing_file_is_named(self):
        self.tls.mkdir()
        (self.tls / "cert.pem").write_text("x")
        err = self.run_script()
        self.assertIn(f"{self.tls}/key.pem is missing", err)
        self.assertNotIn("cannot read", err)

    @unittest.skipIf(os.geteuid() == 0, "root reads everything, so nothing is unreadable")
    def test_unreadable_file_is_distinguished_from_missing(self):
        self.tls.mkdir()
        (self.tls / "cert.pem").write_text("x")
        key = self.tls / "key.pem"
        key.write_text("x")
        key.chmod(0)
        err = self.run_script()
        self.assertIn("key.pem exists but uid", err)
        self.assertIn("cannot read it", err)
        self.assertNotIn("is missing", err)

    @unittest.skipIf(os.geteuid() == 0, "root enters every directory")
    def test_directory_without_execute_bit_is_named_not_reported_as_missing_files(self):
        # The lab trap: files fine, directory 0700 owned by someone else, so the container user cannot enter it.
        self.tls.mkdir()
        (self.tls / "cert.pem").write_text("x")
        (self.tls / "key.pem").write_text("x")
        self.tls.chmod(0)
        self.addCleanup(self.tls.chmod, stat.S_IRWXU)  # let the temp dir be removed
        err = self.run_script()
        self.assertIn("cannot enter it", err)
        self.assertIn("mode 755", err)
        self.assertNotIn("is missing", err)

    def test_error_shows_uid_and_ls_output_for_the_directory(self):
        self.tls.mkdir()
        err = self.run_script()
        self.assertIn(f"runs as uid {os.getuid()}", err)
        self.assertRegex(err, rf"drwx\S+\s+\d+\s+\d+\s+\d+.*{re.escape(str(self.tls))}")  # ls -ldn: numeric owner


if __name__ == "__main__":
    unittest.main()
