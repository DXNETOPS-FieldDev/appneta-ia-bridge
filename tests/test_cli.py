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


if __name__ == "__main__":
    unittest.main()
