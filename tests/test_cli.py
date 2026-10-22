import unittest
from unittest import mock

from alarm_bridge.cli import main


class TestMain(unittest.TestCase):
    def test_missing_config_returns_nonzero_without_starting_server(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch("alarm_bridge.cli.run_server") as run_server:
                exit_code = main([])
        self.assertNotEqual(exit_code, 0)
        run_server.assert_not_called()

    def test_valid_config_starts_server(self):
        env = {
            "TRAP_DESTINATION_1_HOST": "spectrum.example.com",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "NONE",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch("alarm_bridge.cli.run_server") as run_server:
                exit_code = main([])
        self.assertEqual(exit_code, 0)
        run_server.assert_called_once()
        called_config = run_server.call_args[0][0]
        self.assertEqual(called_config.trap_destinations[0].host, "spectrum.example.com")

    def test_trap_destination_flag_overrides_env(self):
        # No TRAP_DESTINATION_* in env at all -- the CLI flag alone must satisfy config.
        env = {"AUTH_TYPE": "NONE"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch("alarm_bridge.cli.run_server") as run_server:
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
            "AUTH_TYPE": "NONE",
            "APPNETA_V3_ID_OFFSET": "42",
        }
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch("alarm_bridge.cli.run_server") as run_server:
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
        env = {"AUTH_TYPE": "NONE", "APPNETA_V3_ID_OFFSET": "42"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch("alarm_bridge.cli.run_server") as run_server:
                main(["--trap-destination", "spectrum:162:public"])
        called_config = run_server.call_args[0][0]
        self.assertEqual(called_config.trap_destinations[0].scheme, "vna")

    def _run_flag(self, value):
        env = {"AUTH_TYPE": "NONE", "APPNETA_V3_ID_OFFSET": "42"}
        with mock.patch.dict("os.environ", env, clear=True):
            with mock.patch("alarm_bridge.cli.run_server") as run_server:
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
        with mock.patch("alarm_bridge.cli.run_server") as run_server:
            with mock.patch("sys.stderr") as err:
                with self.assertRaises(SystemExit) as ctx:
                    main(["--trap-destination", "h:162:c:bogus:192.0.2.3"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("scheme", "".join(c.args[0] for c in err.write.call_args_list))
        run_server.assert_not_called()

    def test_invalid_source_address_is_argparse_error(self):
        with mock.patch("alarm_bridge.cli.run_server") as run_server:
            with mock.patch("sys.stderr") as err:
                with self.assertRaises(SystemExit) as ctx:
                    main(["--trap-destination", "h:162:c:general:999.1.1.1"])
        self.assertEqual(ctx.exception.code, 2)
        stderr = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("invalid SOURCE_ADDRESS", stderr)
        self.assertIn("999.1.1.1", stderr)
        run_server.assert_not_called()


if __name__ == "__main__":
    unittest.main()
