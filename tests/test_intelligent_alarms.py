"""Regression coverage for intelligent_alarms.sh argument handling.

`--spectroserver uninstall` with the mode omitted, which the usage line allows,
must not fail on a "mode required" check. Only install needs a mode;
uninstall removes whatever the state marker says is installed.
"""

import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "intelligent_alarms.sh"

# Keep install logs out of the repo's logs/ directory while testing.
os.environ["IA_LOG_DIR"] = tempfile.mkdtemp(prefix="ia-test-logs-")


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
        result = run(self.home, "--spectroserver", "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Nothing installed", result.stdout)

    def test_install_without_mode_is_rejected(self):
        result = run(self.home, "--spectroserver", "install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stderr)

    def test_status_reports_not_installed(self):
        result = run(self.home, "--spectroserver", "status")
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
        for args in (("--spectroserver", "--vna", "install"), ("--spectroserver", "uninstall"),
                     ("--spectroserver", "status"), ("status",)):
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
        return subprocess.run(["bash", str(SCRIPT), "--spectroserver", "status"],
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
        result = subprocess.run(["bash", str(SCRIPT), "--spectroserver", "status"],
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
        result = run(self.home, "--spectroserver", "--vna", "install")
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
        result = subprocess.run(["bash", str(SCRIPT), "--spectroserver", "--vna", "install"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not 'root'", result.stderr)
        self.assertIn("sudo -u ", result.stderr)
        self.assertIn(f"env SPECTRUM_HOME={self.home}", result.stderr)
        self.assertIn("--spectroserver --vna install", result.stderr)
        self.assertEqual(self.alertmap.read_text(), "existing\n")

    def test_missing_ss_dir_is_reported(self):
        bare = self.home / "elsewhere"
        bare.mkdir()
        result = run(bare, "--spectroserver", "--vna", "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is SPECTRUM_HOME", result.stderr.replace("Is SPECTRUM_HOME", "is SPECTRUM_HOME"))


class TestOneClickHost(unittest.TestCase):
    """Split install: the OneClick-only host has SG-Support but no SS/ tree. --oneclick installs
    just the CsPCause/CsEvFormat text files that host's OneClick reads (confirmed on a split lab)."""

    ROOT = SCRIPT.parent

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name)
        self.pcause = self.home / "SG-Support/CsPCause"
        self.evformat = self.home / "SG-Support/CsEvFormat"
        self.pcause.mkdir(parents=True)
        self.evformat.mkdir(parents=True)
        (self.pcause / "ProbFOREIGN_en_US").write_text("not ours\n")
        self.marker = self.home / "SG-Support/.intelligent-alarms-oneclick-state"

    def tearDown(self):
        self._tmp.cleanup()

    def bundle(self, mode):
        d = self.ROOT / ("spectrum-install" if mode == "vna" else "spectrum-install-general")
        return ({p.name for p in d.glob("Prob*_en_US")}, {p.name for p in d.glob("Event*_en_US")})

    def test_install_copies_exactly_the_bundles_text_files_and_no_ss_tree(self):
        result = run(self.home, "--oneclick", "--general", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        probs, events = self.bundle("general")
        self.assertEqual({p.name for p in self.pcause.iterdir()} - {"ProbFOREIGN_en_US"}, probs)
        self.assertEqual({p.name for p in self.evformat.iterdir()}, events)
        self.assertIn("mode=general", self.marker.read_text())
        self.assertFalse((self.home / "SS").exists())

    def test_status_reports_mode(self):
        self.assertIn("not installed", run(self.home, "--oneclick", "status").stdout)
        run(self.home, "--oneclick", "--vna", "install")
        self.assertIn("vna installed", run(self.home, "--oneclick", "status").stdout)

    def test_uninstall_removes_only_our_files_and_marker(self):
        run(self.home, "--oneclick", "--general", "install")
        result = run(self.home, "--oneclick", "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p.name for p in self.pcause.iterdir()], ["ProbFOREIGN_en_US"])
        self.assertEqual(list(self.evformat.iterdir()), [])
        self.assertFalse(self.marker.exists())

    def test_switching_mode_removes_the_old_modes_files(self):
        run(self.home, "--oneclick", "--general", "install")
        result = run(self.home, "--oneclick", "--vna", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        g_probs, g_events = self.bundle("general")
        v_probs, v_events = self.bundle("vna")
        self.assertEqual({p.name for p in self.pcause.iterdir()} - {"ProbFOREIGN_en_US"}, v_probs)
        self.assertEqual({p.name for p in self.evformat.iterdir()}, v_events)
        self.assertTrue((g_probs | g_events).isdisjoint(v_probs | v_events))

    def test_install_without_mode_is_rejected(self):
        result = run(self.home, "--oneclick", "install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stderr)

    def test_oneclick_with_smarts_is_rejected(self):
        result = run(self.home, "--smarts", "--oneclick", "--general", "install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("choose one of", result.stderr)

    def test_removed_spectrum_flag_points_at_replacements(self):
        for args in (("--spectrum", "--general", "install"), ("--spectrum", "status")):
            result = run(self.home, *args)
            self.assertEqual(result.returncode, 1)
            self.assertIn("--spectroserver", result.stderr)
            self.assertIn("--oneclick", result.stderr)

    def test_plain_spectrum_install_on_oneclick_host_points_at_the_flag(self):
        result = run(self.home, "--spectroserver", "--general", "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--oneclick", result.stderr)
        self.assertEqual({p.name for p in self.evformat.iterdir()}, set())


class TestGeneralUninstallRoundTrip(unittest.TestCase):
    """general install then uninstall must leave no Event0673217* behind (9 and b, the bare-declared
    MINOR/WARNING CLEARED codes, were once missing from the removal list)."""

    def test_no_event_files_left_after_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "SS/CsVendor/SDN_INTEG/AppNetaAdmin").mkdir(parents=True)
            (home / "SG-Support/CsPCause").mkdir(parents=True)
            (home / "SG-Support/CsEvFormat").mkdir(parents=True)
            self.assertEqual(run(home, "--spectroserver", "--general", "install").returncode, 0)
            self.assertEqual(run(home, "--spectroserver", "uninstall").returncode, 0)
            self.assertEqual(list((home / "SG-Support/CsEvFormat").iterdir()), [])
            self.assertEqual(list((home / "SG-Support/CsPCause").iterdir()), [])


class TestMibCompile(unittest.TestCase):
    """The installers run Spectrum's BulkMibCompile.sh when this host has a MibDatabase. That
    script exits 0 even when a MIB fails to parse (observed on a lab), so failure is read from its
    output."""

    OK = ("Compiling : APPNETA-MIB.mib\nCompiling : APPNETA-INTELLIGENT-ALARM-MIB.mib\n")
    BAD = "Compiling : APPNETA-INTELLIGENT-ALARM-MIB.mib\nMIB failed to compile.\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name)
        (self.home / "SG-Support/CsPCause").mkdir(parents=True)
        (self.home / "SG-Support/CsEvFormat").mkdir(parents=True)
        self.seen = self.home / "seen.txt"

    def tearDown(self):
        self._tmp.cleanup()

    def fake_compiler(self, output, rc=0):
        d = self.home / "MibDatabase/scripts"
        d.mkdir(parents=True)
        (d / "BulkMibCompile.sh").write_text(
            f'#!/bin/bash\nls "$2" > {self.seen}\ncat <<\'EOT\'\n{output}EOT\nexit {rc}\n'
        )

    def test_oneclick_install_compiles_both_mibs(self):
        self.fake_compiler(self.OK)
        result = run(self.home, "--oneclick", "--general", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.seen.read_text().split(), ["APPNETA-INTELLIGENT-ALARM-MIB.mib", "APPNETA-MIB.mib"])
        self.assertIn("MIBs compiled", result.stdout)

    def test_compile_failure_with_exit_zero_fails_the_install(self):
        self.fake_compiler(self.BAD)
        result = run(self.home, "--oneclick", "--general", "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("MIB compile failed", result.stderr)

    def test_nonzero_exit_fails_the_install(self):
        self.fake_compiler(self.OK, rc=3)
        self.assertEqual(run(self.home, "--oneclick", "--general", "install").returncode, 1)

    def test_no_mibdatabase_skips_the_compile(self):
        result = run(self.home, "--oneclick", "--general", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipping the MIB compile", result.stdout)

    def test_spectroserver_install_compiles_too(self):
        (self.home / "SS/CsVendor/SDN_INTEG/AppNetaAdmin").mkdir(parents=True)
        self.fake_compiler(self.BAD)
        result = run(self.home, "--spectroserver", "--vna", "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("MIB compile failed", result.stderr)


class TestLogging(unittest.TestCase):
    """install/uninstall write a <yyyymmddhhmm>-<action>.log under the logs directory, creating it if needed;
    status does not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name) / "home"
        (self.home / "SG-Support/CsPCause").mkdir(parents=True)
        (self.home / "SG-Support/CsEvFormat").mkdir(parents=True)
        self.logdir = pathlib.Path(self._tmp.name) / "newlogs"   # does not exist yet

    def tearDown(self):
        self._tmp.cleanup()

    def run_logged(self, *args, log_dir=None):
        env = dict(os.environ, SPECTRUM_HOME=str(self.home), IA_LOG_DIR=str(log_dir or self.logdir))
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)

    def test_install_creates_the_log_directory_and_a_dated_log(self):
        result = self.run_logged("--oneclick", "--general", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        logs = list(self.logdir.glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertRegex(logs[0].name, r"^\d{12}-install\.log$")
        text = logs[0].read_text()
        self.assertIn("args: --oneclick --general install", text)
        self.assertIn("OneClick-host files installed", text)
        self.assertIn("exit status 0", text)
        self.assertIn(str(logs[0]), result.stdout)

    def test_failed_install_log_records_the_error_and_status(self):
        (self.home / "MibDatabase/scripts").mkdir(parents=True)
        (self.home / "MibDatabase/scripts/BulkMibCompile.sh").write_text(
            "#!/bin/bash\necho 'MIB failed to compile.'\n")
        result = self.run_logged("--oneclick", "--general", "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("MIB compile failed", result.stderr)
        text = next(self.logdir.glob("*.log")).read_text()
        self.assertIn("MIB compile failed", text)
        self.assertIn("exit status 1", text)

    def test_uninstall_log_is_named_for_the_action(self):
        self.run_logged("--oneclick", "--general", "install")
        self.run_logged("--oneclick", "uninstall")
        names = sorted(p.name.split("-", 1)[1] for p in self.logdir.glob("*.log"))
        self.assertEqual(names, ["install.log", "uninstall.log"])

    def test_same_minute_run_appends_to_the_existing_log(self):
        fakebin = pathlib.Path(self._tmp.name) / "bin"
        fakebin.mkdir()
        real_date = subprocess.run(["which", "date"], capture_output=True, text=True).stdout.strip()
        fake = fakebin / "date"
        fake.write_text(f'#!/bin/bash\n[ "$1" = "+%Y%m%d%H%M" ] && {{ echo 202601011200; exit; }}\nexec {real_date} "$@"\n')
        fake.chmod(0o755)
        env = dict(os.environ, SPECTRUM_HOME=str(self.home), IA_LOG_DIR=str(self.logdir),
                   PATH=f"{fakebin}:{os.environ['PATH']}")
        for _ in range(2):
            subprocess.run(["bash", str(SCRIPT), "--oneclick", "--general", "install"],
                           capture_output=True, text=True, env=env)
        logs = list(self.logdir.glob("*.log"))
        self.assertEqual([p.name for p in logs], ["202601011200-install.log"])
        text = logs[0].read_text()
        self.assertEqual(text.count("=== intelligent_alarms.sh oneclick-install:"), 2)
        self.assertEqual(text.count("exit status 0"), 2)

    def test_symlink_at_the_log_name_is_not_followed(self):
        self.logdir.mkdir()
        victim = pathlib.Path(self._tmp.name) / "victim.txt"
        victim.write_text("keep\n")
        victim.chmod(0o600)
        env = dict(os.environ, SPECTRUM_HOME=str(self.home), IA_LOG_DIR=str(self.logdir))
        # pin the minute so the planted link has the name the script will pick
        fakebin = pathlib.Path(self._tmp.name) / "bin"
        fakebin.mkdir()
        real_date = subprocess.run(["which", "date"], capture_output=True, text=True).stdout.strip()
        fake = fakebin / "date"
        fake.write_text(f'#!/bin/bash\n[ "$1" = "+%Y%m%d%H%M" ] && {{ echo 202601011200; exit; }}\nexec {real_date} "$@"\n')
        fake.chmod(0o755)
        env["PATH"] = f"{fakebin}:{os.environ['PATH']}"
        (self.logdir / "202601011200-install.log").symlink_to(victim)
        result = subprocess.run(["bash", str(SCRIPT), "--oneclick", "--general", "install"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("cannot safely write log file", result.stderr)
        self.assertEqual(victim.read_text(), "keep\n")
        self.assertEqual(victim.stat().st_mode & 0o777, 0o600)

    def test_new_log_is_not_world_readable(self):
        self.run_logged("--oneclick", "--general", "install")
        log = next(self.logdir.glob("*.log"))
        self.assertEqual(log.stat().st_mode & 0o007, 0)
        self.assertEqual(self.logdir.stat().st_mode & 0o007, 0)

    def test_status_writes_no_log(self):
        self.run_logged("--oneclick", "status")
        self.assertFalse(self.logdir.exists())

    def test_unwritable_log_directory_warns_and_the_install_still_runs(self):
        blocker = pathlib.Path(self._tmp.name) / "afile"
        blocker.write_text("x")
        result = self.run_logged("--oneclick", "--general", "install", log_dir=blocker / "logs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("continuing without a log", result.stderr)


class TestReviewFixes(unittest.TestCase):
    """Regressions from the code review: silent exits on a damaged marker, half-done uninstalls,
    status that missed the OneClick marker, and logging side effects."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self._tmp.name) / "home"
        self.vendor = self.home / "SS/CsVendor/SDN_INTEG/AppNetaAdmin"
        self.vendor.mkdir(parents=True)
        (self.home / "SG-Support/CsPCause").mkdir(parents=True)
        (self.home / "SG-Support/CsEvFormat").mkdir(parents=True)
        self.marker = self.vendor / ".intelligent-alarms-state"
        self.oc_marker = self.home / "SG-Support/.intelligent-alarms-oneclick-state"

    def tearDown(self):
        self._tmp.cleanup()

    def install_general_over(self, alertmap="existing\n", eventdisp="existing\n"):
        (self.vendor / "AlertMap").write_text(alertmap)
        (self.vendor / "EventDisp").write_text(eventdisp)
        self.assertEqual(run(self.home, "--spectroserver", "--general", "install").returncode, 0)

    def test_uninstall_with_a_marker_missing_fields_says_so(self):
        self.marker.write_text("mode=general\n")
        result = run(self.home, "--spectroserver", "uninstall")
        self.assertEqual(result.returncode, 1)
        self.assertIn("malformed", result.stderr)

    def test_marker_without_mode_is_not_reported_as_not_installed(self):
        self.marker.write_text("installed=today\n")
        result = run(self.home, "--spectroserver", "status")
        self.assertIn("unreadable", result.stdout)
        self.assertNotIn("not installed", result.stdout)

    def test_oneclick_uninstall_with_no_mode_in_marker_says_so(self):
        self.oc_marker.write_text("installed=today\n")
        result = run(self.home, "--oneclick", "uninstall")
        self.assertEqual(result.returncode, 1)
        self.assertIn("manual reconciliation", result.stderr)

    def test_uninstall_changes_nothing_when_the_second_file_fails_its_check(self):
        self.install_general_over()
        alertmap_after_install = (self.vendor / "AlertMap").read_text()
        with open(self.vendor / "EventDisp", "a") as f:
            f.write("hand edit\n")
        result = run(self.home, "--spectroserver", "uninstall")
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.vendor / "AlertMap").read_text(), alertmap_after_install)
        self.assertTrue(self.marker.exists())

    def test_uninstall_keeps_the_files_inode_and_mode(self):
        self.install_general_over()
        alertmap = self.vendor / "AlertMap"
        alertmap.chmod(0o664)
        inode = alertmap.stat().st_ino
        self.assertEqual(run(self.home, "--spectroserver", "uninstall").returncode, 0)
        self.assertEqual(alertmap.read_text(), "existing\n")
        self.assertEqual(alertmap.stat().st_ino, inode)
        self.assertEqual(alertmap.stat().st_mode & 0o777, 0o664)

    def test_bare_status_includes_the_oneclick_host(self):
        shutil.rmtree(self.home / "SS")   # a OneClick-only host
        self.assertEqual(run(self.home, "--oneclick", "--general", "install").returncode, 0)
        result = run(self.home, "status")
        self.assertIn("OneClick host: general installed", result.stdout)

    def test_bare_status_stays_quiet_about_oneclick_when_not_installed(self):
        self.assertNotIn("OneClick", run(self.home, "status").stdout)

    def test_root_refusal_does_not_create_the_log_directory(self):
        stub = pathlib.Path(self._tmp.name) / "stub"
        stub.mkdir()
        (stub / "id").write_text("#!/bin/sh\necho root\n")
        (stub / "id").chmod(0o755)
        logdir = pathlib.Path(self._tmp.name) / "logs-as-root"
        env = dict(os.environ, SPECTRUM_HOME=str(self.home), IA_LOG_DIR=str(logdir),
                   PATH=f"{stub}:{os.environ['PATH']}")
        result = subprocess.run(["bash", str(SCRIPT), "--spectroserver", "--vna", "install"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not 'root'", result.stderr)
        self.assertFalse(logdir.exists())

    def test_compile_fails_cleanly_when_mktemp_fails(self):
        shutil.rmtree(self.home / "SS")   # a OneClick-only host
        d = self.home / "MibDatabase/scripts"
        d.mkdir(parents=True)
        (d / "BulkMibCompile.sh").write_text("#!/bin/bash\necho Compiling : APPNETA-INTELLIGENT-ALARM-MIB.mib\n")
        stub = pathlib.Path(self._tmp.name) / "stub"
        stub.mkdir()
        (stub / "mktemp").write_text("#!/bin/sh\nexit 1\n")   # what a full or read-only /tmp does
        (stub / "mktemp").chmod(0o755)
        env = dict(os.environ, SPECTRUM_HOME=str(self.home), PATH=f"{stub}:{os.environ['PATH']}")
        result = subprocess.run(["bash", str(SCRIPT), "--oneclick", "--general", "install"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("scratch directory", result.stderr)

    def _vna_markers(self):
        return (self.vendor / ".intelligent-alarms-state", self.home / "SG-Support/.intelligent-alarms-oneclick-state")

    def test_oneclick_install_is_refused_on_a_spectroserver_host(self):
        result = run(self.home, "--oneclick", "--general", "install")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--spectroserver", result.stderr)
        self.assertFalse(self._vna_markers()[1].exists())

    def test_oneclick_uninstall_is_refused_when_spectroserver_is_installed(self):
        self.vendor.joinpath(".intelligent-alarms-state").write_text("mode=general\n")
        self.oc_marker.parent.mkdir(parents=True, exist_ok=True)
        self.oc_marker.write_text("mode=general\n")
        result = run(self.home, "--oneclick", "uninstall")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.oc_marker.exists())

    def test_stray_oneclick_marker_on_an_ss_host_can_still_be_uninstalled(self):
        self.oc_marker.parent.mkdir(parents=True, exist_ok=True)
        self.oc_marker.write_text("mode=general\n")
        result = run(self.home, "--oneclick", "uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.oc_marker.exists())

    def test_status_flags_an_incomplete_install_and_not_a_finished_one(self):
        self.vendor.joinpath(".intelligent-alarms-state").write_text("mode=vna\nstate=incomplete\n")
        self.assertIn("INCOMPLETE", run(self.home, "--spectroserver", "status").stdout)
        self.vendor.joinpath(".intelligent-alarms-state").write_text("mode=vna\nstate=complete\n")
        self.assertNotIn("INCOMPLETE", run(self.home, "--spectroserver", "status").stdout)
        self.vendor.joinpath(".intelligent-alarms-state").write_text("mode=vna\n")   # pre-field marker
        self.assertNotIn("INCOMPLETE", run(self.home, "--spectroserver", "status").stdout)

    def test_oneclick_status_flags_an_incomplete_install(self):
        shutil.rmtree(self.home / "SS")
        self.oc_marker.parent.mkdir(parents=True, exist_ok=True)
        self.oc_marker.write_text("mode=general\nstate=incomplete\n")
        self.assertIn("INCOMPLETE", run(self.home, "--oneclick", "status").stdout)

    def _failing_compiler(self):
        d = self.home / "MibDatabase/scripts"
        d.mkdir(parents=True)
        (d / "BulkMibCompile.sh").write_text("#!/bin/bash\necho MIB failed to compile\n")

    def test_failed_compile_leaves_the_marker_incomplete_and_success_completes_it(self):
        self._failing_compiler()
        self.assertEqual(run(self.home, "--spectroserver", "--general", "install").returncode, 1)
        self.assertIn("state=incomplete", self._vna_markers()[0].read_text())
        self.assertIn("INCOMPLETE", run(self.home, "--spectroserver", "status").stdout)
        (self.home / "MibDatabase/scripts/BulkMibCompile.sh").write_text(
            "#!/bin/bash\necho 'Compiling : APPNETA-INTELLIGENT-ALARM-MIB.mib'\n")
        self.assertEqual(run(self.home, "--spectroserver", "--general", "install").returncode, 0)
        text = self._vna_markers()[0].read_text()
        self.assertIn("state=complete", text)
        self.assertNotIn("state=incomplete", text)

    def test_failed_oneclick_compile_leaves_the_marker_incomplete(self):
        shutil.rmtree(self.home / "SS")
        self._failing_compiler()
        self.assertEqual(run(self.home, "--oneclick", "--general", "install").returncode, 1)
        self.assertIn("state=incomplete", self.oc_marker.read_text())

    def test_spectroserver_install_reminds_about_oneclick_when_it_is_not_on_this_host(self):
        result = run(self.home, "--spectroserver", "--general", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REMINDER: OneClick is not installed on this host", result.stdout)
        self.assertIn("--oneclick --general install", result.stdout)

    def test_spectroserver_install_is_quiet_when_oneclick_is_on_this_host(self):
        (self.home / "tomcat").mkdir()
        result = run(self.home, "--spectroserver", "--general", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("REMINDER", result.stdout)


if __name__ == "__main__":
    unittest.main()
