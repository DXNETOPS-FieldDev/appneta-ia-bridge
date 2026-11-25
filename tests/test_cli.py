import contextlib
import io
import logging
import os
import tempfile
import unittest
from unittest import mock

from alarm_bridge.cli import main
from alarm_bridge.obs import shutdown_logging


class _RestoresLogging(unittest.TestCase):
    """main() configures the root logger; undo it so other test modules see no change."""

    def setUp(self):
        root = logging.getLogger()
        saved = (root.level, list(root.handlers))

        def restore():
            shutdown_logging()
            root.setLevel(saved[0])
            root.handlers[:] = saved[1]

        self.addCleanup(restore)


class TestMain(_RestoresLogging):
    def test_missing_config_returns_nonzero_without_starting_server(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("alarm_bridge.cli.run_server") as run_server:
            exit_code = main([])
        self.assertNotEqual(exit_code, 0)
        run_server.assert_not_called()

    def test_valid_config_starts_server(self):
        env = {
            "TRAP_DESTINATION_1_HOST": "spectrum.example.com",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
        }
        with mock.patch.dict("os.environ", env, clear=True), mock.patch("alarm_bridge.cli.run_server") as run_server:
            exit_code = main([])
        self.assertEqual(exit_code, 0)
        run_server.assert_called_once()
        called_config = run_server.call_args[0][0]
        self.assertEqual(called_config.trap_destinations[0].host, "spectrum.example.com")

    def test_trap_destination_flag_overrides_env(self):
        # No TRAP_DESTINATION_* in env at all -- the CLI flag alone must satisfy config.
        env = {"AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token"}
        with mock.patch.dict("os.environ", env, clear=True), mock.patch("alarm_bridge.cli.run_server") as run_server:
            exit_code = main(["--trap-destination", "smarts.example.com:162:public:general"])
        self.assertEqual(exit_code, 0)
        called_config = run_server.call_args[0][0]
        [dest] = called_config.trap_destinations
        self.assertEqual(dest.host, "smarts.example.com")
        self.assertEqual(dest.port, 162)
        self.assertEqual(dest.community, "public")
        self.assertEqual(dest.scheme, "general")

    def test_repeated_trap_destination_flags_are_not_merged_with_env(self):
        env = {
            "TRAP_DESTINATION_1_HOST": "ignored-env-host",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            "APPNETA_V3_ID_OFFSET": "42",
        }
        with mock.patch.dict("os.environ", env, clear=True), mock.patch("alarm_bridge.cli.run_server") as run_server:
            exit_code = main([
                "--trap-destination", "spectrum:162:public:vna",
                "--trap-destination", "smarts:162:public:general",
            ])
        self.assertEqual(exit_code, 0)
        called_config = run_server.call_args[0][0]
        self.assertEqual(len(called_config.trap_destinations), 2)
        self.assertEqual(called_config.trap_destinations[0].host, "spectrum")
        self.assertEqual(called_config.trap_destinations[1].host, "smarts")

    def test_trap_destination_flag_scheme_defaults_to_vna(self):
        env = {"AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token", "APPNETA_V3_ID_OFFSET": "42"}
        with mock.patch.dict("os.environ", env, clear=True), mock.patch("alarm_bridge.cli.run_server") as run_server:
            main(["--trap-destination", "spectrum:162:public"])
        called_config = run_server.call_args[0][0]
        self.assertEqual(called_config.trap_destinations[0].scheme, "vna")

    def _run_flag(self, value):
        env = {"AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token", "APPNETA_V3_ID_OFFSET": "42"}
        with mock.patch.dict("os.environ", env, clear=True), mock.patch("alarm_bridge.cli.run_server") as run_server:
            main(["--trap-destination", value])
        return run_server.call_args[0][0].trap_destinations[0]

    def test_five_field_destination_sets_source_address(self):
        dest = self._run_flag("h:162:c:general:192.0.2.3")
        self.assertEqual(dest.source_address, "192.0.2.3")
        self.assertEqual(dest.scheme, "general")

    def test_four_field_destination_has_no_claim(self):
        self.assertEqual(self._run_flag("h:162:c:general").source_address, "")

    def test_none_source_address_is_opt_out(self):
        dest = self._run_flag("h:162:c:general:none")
        self.assertEqual(dest.source_address, "")
        self.assertEqual(dest.scheme, "general")

    def test_bogus_scheme_with_source_address_is_argparse_error(self):
        with mock.patch("alarm_bridge.cli.run_server") as run_server, mock.patch("sys.stderr") as err, self.assertRaises(SystemExit) as ctx:
            main(["--trap-destination", "h:162:c:bogus:192.0.2.3"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("scheme", "".join(c.args[0] for c in err.write.call_args_list))
        run_server.assert_not_called()

    def test_invalid_source_address_is_argparse_error(self):
        with mock.patch("alarm_bridge.cli.run_server") as run_server, mock.patch("sys.stderr") as err, self.assertRaises(SystemExit) as ctx:
            main(["--trap-destination", "h:162:c:general:999.1.1.1"])
        self.assertEqual(ctx.exception.code, 2)
        stderr = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("invalid SOURCE_ADDRESS", stderr)
        self.assertIn("999.1.1.1", stderr)
        run_server.assert_not_called()

class TestMainLogging(_RestoresLogging):
    def test_invalid_log_level_returns_1_with_error_and_no_traceback(self):
        err = io.StringIO()
        env = {"LOG_LEVEL": "LOUD", "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "t"}
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch("alarm_bridge.cli.run_server") as run_server, \
                contextlib.redirect_stderr(err):
            exit_code = main([])
        self.assertEqual(exit_code, 1)
        self.assertIn("ERROR:", err.getvalue())
        self.assertIn("LOG_LEVEL", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        run_server.assert_not_called()

    def test_log_level_applied_and_syslog_configured_from_config(self):
        env = {
            "TRAP_DESTINATION_1_HOST": "spectrum.example.com",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            "LOG_LEVEL": "warning",
            "LOG_SYSLOG_ADDRESS": "127.0.0.1:1514", "LOG_SYSLOG_PROTOCOL": "tcp",
        }
        with mock.patch.dict("os.environ", env, clear=True), \
                mock.patch("alarm_bridge.cli.run_server"), \
                mock.patch("alarm_bridge.cli.setup_logging") as setup:
            self.assertEqual(main([]), 0)
        self.assertEqual(setup.call_args_list, [
            mock.call("WARNING"),
            mock.call("WARNING", [("127.0.0.1", 1514)], "tcp"),
        ])


_SPOOL_ENV = {
    "TRAP_DESTINATION_1_HOST": "127.0.0.1",
    "TRAP_DESTINATION_1_COMMUNITY": "public",
    "TRAP_DESTINATION_1_SCHEME": "general",
    "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
    "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
}


class TestMainSpoolStartup(_RestoresLogging):
    @unittest.skipIf(os.geteuid() == 0, "root can write a 0500 directory")
    def test_run_server_refuses_to_start_when_state_dir_unwritable(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        state = os.path.join(tmp.name, "state")
        os.mkdir(state, 0o500)
        self.addCleanup(os.chmod, state, 0o700)
        err = io.StringIO()
        with mock.patch.dict("os.environ", {**_SPOOL_ENV, "DEDUP_STATE_DIR": state}, clear=True), \
                mock.patch("alarm_bridge.server.ThreadingHTTPServer") as server_cls, \
                contextlib.redirect_stderr(err):
            exit_code = main([])
        self.assertNotEqual(exit_code, 0)
        server_cls.assert_not_called()
        self.assertIn(f"ERROR: state directory {state}", err.getvalue())
        self.assertIn("10001", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_logging_is_shut_down_after_the_server_returns(self):
        with mock.patch.dict("os.environ", _SPOOL_ENV, clear=True), \
                mock.patch("alarm_bridge.cli.run_server", return_value=None), \
                mock.patch("alarm_bridge.cli.shutdown_logging") as shutdown:
            self.assertEqual(main([]), 0)
        shutdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
