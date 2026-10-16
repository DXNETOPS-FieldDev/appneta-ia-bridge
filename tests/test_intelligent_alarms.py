"""Regression coverage for intelligent_alarms.sh argument handling.

`--spectrum uninstall` with the mode omitted, which the usage line allows,
must not fail on a "mode required" check. Only install needs a mode;
uninstall removes whatever the state marker says is installed.
"""

import os
import pathlib
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "intelligent_alarms.sh"


def run(spectrum_home, *args):
    env = dict(os.environ, SPECTRUM_HOME=str(spectrum_home))
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env
    )


class TestDispatcherArguments(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name)
        (self.home / "SS/CsVendor/SDN_INTEG/AppNetaAdmin").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_uninstall_without_mode_is_accepted(self):
        result = run(self.home, "--spectrum", "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing installed", result.stdout)

    def test_install_without_mode_is_rejected(self):
        result = run(self.home, "--spectrum", "install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stderr)

    def test_status_reports_not_installed(self):
        result = run(self.home, "--spectrum", "status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Spectrum: not installed", result.stdout)


class TestSpectrumHomeRequired(unittest.TestCase):
    """SPECTRUM_HOME has no default; unset must stop with a clear message."""

    def run_unset(self, *args):
        env = {k: v for k, v in os.environ.items() if k != "SPECTRUM_HOME"}
        env["SPECTRUM_ENV_FILE"] = "/nonexistent/spectrum80.env"
        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env
        )

    def test_spectrum_actions_stop_when_unset(self):
        for args in (("--spectrum", "--vna", "install"), ("--spectrum", "uninstall"),
                     ("--spectrum", "status"), ("status",)):
            result = self.run_unset(*args)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn("SPECTRUM_HOME is not set", result.stderr, args)

    def test_smarts_does_not_need_spectrum_home(self):
        result = self.run_unset("--smarts", "status")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lib_scripts_stop_when_unset(self):
        lib = SCRIPT.parent / "lib"
        for name in ("install-spectrum-vna.sh", "install-spectrum-general.sh",
                     "uninstall-spectrum.sh"):
            env = {k: v for k, v in os.environ.items() if k != "SPECTRUM_HOME"}
            env["SPECTRUM_ENV_FILE"] = "/nonexistent/spectrum80.env"
            result = subprocess.run(["bash", str(lib / name)], capture_output=True,
                                    text=True, env=env)
            self.assertNotEqual(result.returncode, 0, name)
            self.assertIn("SPECTRUM_HOME is not set", result.stderr, name)


class TestSpectrumEnvFile(unittest.TestCase):
    """With SPECTRUM_HOME unset, SPECROOT from spectrum80.env supplies it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.home = self.root / "Spectrum"
        (self.home / "SS/CsVendor/SDN_INTEG/AppNetaAdmin").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def run_with_env_file(self, content, **extra):
        env_file = self.root / "spectrum80.env"
        env_file.write_text(content)
        env = {k: v for k, v in os.environ.items() if k != "SPECTRUM_HOME"}
        env.update(SPECTRUM_ENV_FILE=str(env_file), **extra)
        return subprocess.run(["bash", str(SCRIPT), "--spectrum", "status"],
                              capture_output=True, text=True, env=env)

    def test_specroot_supplies_spectrum_home(self):
        result = self.run_with_env_file(f"SPECROOT={self.home}\nPERL=/x/perl\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Using SPECTRUM_HOME={self.home}", result.stderr)
        self.assertIn("Spectrum: not installed", result.stdout)

    def test_quotes_export_and_trailing_slash_are_tolerated(self):
        result = self.run_with_env_file(f'export SPECROOT="{self.home}/"\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Using SPECTRUM_HOME={self.home} ", result.stderr)

    def test_explicit_spectrum_home_wins(self):
        other = self.root / "Other"
        (other / "SS").mkdir(parents=True)
        result = self.run_with_env_file(f"SPECROOT={self.home}\n", SPECTRUM_HOME=str(other))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Using SPECTRUM_HOME", result.stderr)

    def test_duplicate_identical_lines_are_fine(self):
        result = self.run_with_env_file(f"SPECROOT={self.home}\nSPECROOT={self.home}\n")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_conflicting_specroot_is_refused(self):
        result = self.run_with_env_file(f"SPECROOT={self.home}\nSPECROOT=/somewhere/else\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("conflicting", result.stderr)

    def test_no_specroot_line_is_refused(self):
        result = self.run_with_env_file("PERL=/x/perl\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no SPECROOT=", result.stderr)

    def test_specroot_not_a_directory_is_refused(self):
        result = self.run_with_env_file("SPECROOT=/nonexistent/Spectrum\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a directory", result.stderr)

    def test_missing_env_file_names_both_remedies(self):
        env = {k: v for k, v in os.environ.items() if k != "SPECTRUM_HOME"}
        env["SPECTRUM_ENV_FILE"] = str(self.root / "nope.env")
        result = subprocess.run(["bash", str(SCRIPT), "--spectrum", "status"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("SPECTRUM_HOME", result.stderr)
        self.assertIn("SPECTRUM_ENV_FILE", result.stderr)


@unittest.skipIf(os.geteuid() == 0, "writability checks are meaningless as root")
class TestPreflight(unittest.TestCase):
    """Ownership/permission problems must stop the run before anything is modified."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name)
        self.vendor = self.home / "SS/CsVendor/SDN_INTEG/AppNetaAdmin"
        self.vendor.mkdir(parents=True)
        (self.home / "SG-Support/CsPCause").mkdir(parents=True)
        (self.home / "SG-Support/CsEvFormat").mkdir(parents=True)
        self.alertmap = self.vendor / "AlertMap"
        self.alertmap.write_text("existing\n")

    def tearDown(self):
        self.alertmap.chmod(0o644)
        self._tmp.cleanup()

    def test_unwritable_file_stops_before_any_change(self):
        self.alertmap.chmod(0o444)
        result = run(self.home, "--spectrum", "--vna", "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn(str(self.alertmap), result.stderr)
        self.assertIn("chmod u+rw", result.stderr)
        self.assertIn("without sudo", result.stderr)
        self.assertEqual(self.alertmap.read_text(), "existing\n")
        self.assertFalse((self.vendor / ".intelligent-alarms-state").exists())

    def test_root_is_refused_with_corrected_command(self):
        stub = self.home / "stub"
        stub.mkdir()
        fake_id = stub / "id"
        fake_id.write_text("#!/bin/sh\necho root\n")
        fake_id.chmod(0o755)
        env = dict(os.environ, SPECTRUM_HOME=str(self.home), PATH=f"{stub}:{os.environ['PATH']}")
        result = subprocess.run(["bash", str(SCRIPT), "--spectrum", "--vna", "install"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not 'root'", result.stderr)
        self.assertIn("sudo -u ", result.stderr)
        self.assertIn(f"env SPECTRUM_HOME={self.home}", result.stderr)
        self.assertIn("--spectrum --vna install", result.stderr)
        self.assertEqual(self.alertmap.read_text(), "existing\n")

    def test_missing_ss_dir_is_reported(self):
        bare = self.home / "elsewhere"
        bare.mkdir()
        result = run(bare, "--spectrum", "--vna", "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is SPECTRUM_HOME", result.stderr.replace("Is SPECTRUM_HOME", "is SPECTRUM_HOME"))


if __name__ == "__main__":
    unittest.main()
