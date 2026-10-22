import tempfile
import unittest
from pathlib import Path

from alarm_bridge.config import Config, ConfigError, TrapDestination, load_config


class TestLoadConfig(unittest.TestCase):
    def test_minimal_valid_env(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "spectrum.example.com",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "NONE",
        })
        self.assertEqual(config.listen_port, 8080)
        self.assertEqual(config.auth_type, "NONE")
        self.assertEqual(
            config.trap_destinations,
            [TrapDestination(host="spectrum.example.com", port=162, community="public", scheme="general")],
        )

    def test_missing_trap_destination_1_host_raises(self):
        with self.assertRaises(ConfigError):
            load_config({"AUTH_TYPE": "NONE"})

    def test_missing_auth_type_raises(self):
        # AUTH_TYPE has no default -- an operator must consciously choose NONE
        # rather than silently deploying an unauthenticated, internet-facing
        # webhook endpoint.
        with self.assertRaises(ConfigError):
            load_config({"TRAP_DESTINATION_1_HOST": "s"})

    def test_bearer_without_token_raises(self):
        with self.assertRaises(ConfigError):
            load_config({"TRAP_DESTINATION_1_HOST": "s", "AUTH_TYPE": "BEARER"})

    def test_bearer_with_token_succeeds(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER",
            "AUTH_TOKEN": "secret",
        })
        self.assertEqual(config.auth_token, "secret")

    def test_basic_without_password_raises(self):
        with self.assertRaises(ConfigError):
            load_config({
                "TRAP_DESTINATION_1_HOST": "s", "AUTH_TYPE": "BASIC", "AUTH_USERNAME": "svc",
            })

    def test_bearer_token_read_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = Path(tmpdir) / "token"
            token_path.write_text("secret-from-file\n")
            config = load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_SCHEME": "general",
                "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
                "AUTH_TYPE": "BEARER",
                "AUTH_TOKEN_FILE": str(token_path),
            })
        self.assertEqual(config.auth_token, "secret-from-file")

    def test_auth_token_file_takes_precedence_over_auth_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            token_path = Path(tmpdir) / "token"
            token_path.write_text("from-file")
            config = load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_SCHEME": "general",
                "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
                "AUTH_TYPE": "BEARER",
                "AUTH_TOKEN": "from-env",
                "AUTH_TOKEN_FILE": str(token_path),
            })
        self.assertEqual(config.auth_token, "from-file")

    def test_missing_auth_token_file_raises(self):
        with self.assertRaises(ConfigError):
            load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "AUTH_TYPE": "BEARER",
                "AUTH_TOKEN_FILE": "/nonexistent/path/token",
            })

    def test_basic_password_read_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            password_path = Path(tmpdir) / "password"
            password_path.write_text("hunter2\n")
            config = load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_SCHEME": "general",
                "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
                "AUTH_TYPE": "BASIC",
                "AUTH_USERNAME": "svc",
                "AUTH_PASSWORD_FILE": str(password_path),
            })
        self.assertEqual(config.auth_password, "hunter2")

    def test_listen_port_override_applied(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "NONE",
            "LISTEN_PORT": "9090",
        })
        self.assertEqual(config.listen_port, 9090)

    def test_v3_id_offset_defaults_to_none_for_general_scheme(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "NONE",
        })
        self.assertIsNone(config.v3_id_offset)

    def test_vna_scheme_requires_offset(self):
        # The v4->v3 offset is fixed per AppNeta platform host, not universal
        # (2^41 on one host, 2^40 on another), so there is no safe default.
        # The `vna` scheme sends 32-bit IDs and therefore always requires an offset.
        with self.assertRaises(ConfigError):
            load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_SCHEME": "vna",
                "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
                "AUTH_TYPE": "NONE",
            })

    def test_vna_scheme_with_offset_succeeds(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "NONE",
            "APPNETA_V3_ID_OFFSET": "2199023255552",
        })
        self.assertEqual(config.v3_id_offset, 2199023255552)

    def test_vna_scheme_with_known_nis_server_succeeds(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "NONE",
            "APPNETA_NIS_SERVER": "app-12",
        })
        self.assertEqual(config.v3_id_offset, 10995116277760)

    def test_vna_scheme_with_unknown_nis_server_raises(self):
        # app-99 isn't a real/surveyed NIS server -- must fail closed, never
        # silently fall back to no offset (a wrong offset mis-correlates alarms).
        with self.assertRaises(ConfigError):
            load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_SCHEME": "vna",
                "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
                "AUTH_TYPE": "NONE",
                "APPNETA_NIS_SERVER": "app-99",
            })

    def test_explicit_offset_takes_precedence_over_nis_server(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "NONE",
            "APPNETA_NIS_SERVER": "app-12",
            "APPNETA_V3_ID_OFFSET": "42",
        })
        self.assertEqual(config.v3_id_offset, 42)


class TestTrapDestinations(unittest.TestCase):
    def test_single_destination_defaults(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "spectrum.example.com",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "NONE",
            "APPNETA_V3_ID_OFFSET": "42",
        })
        [dest] = config.trap_destinations
        self.assertEqual(dest.host, "spectrum.example.com")
        self.assertEqual(dest.port, 162)
        self.assertEqual(dest.community, "public")
        self.assertEqual(dest.scheme, "vna")

    def test_single_destination_overrides(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_PORT": "1620",
            "TRAP_DESTINATION_1_COMMUNITY": "private",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "NONE",
        })
        [dest] = config.trap_destinations
        self.assertEqual(dest.port, 1620)
        self.assertEqual(dest.community, "private")
        self.assertEqual(dest.scheme, "general")

    def test_two_destinations_parsed(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "spectrum",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "TRAP_DESTINATION_2_HOST": "smarts",
            "TRAP_DESTINATION_2_SCHEME": "general",
            "TRAP_DESTINATION_2_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "NONE",
            "APPNETA_V3_ID_OFFSET": "42",
        })
        self.assertEqual(len(config.trap_destinations), 2)
        self.assertEqual(config.trap_destinations[0].host, "spectrum")
        self.assertEqual(config.trap_destinations[0].scheme, "vna")
        self.assertEqual(config.trap_destinations[1].host, "smarts")
        self.assertEqual(config.trap_destinations[1].scheme, "general")

    def test_stops_at_first_gap(self):
        # _2_ is absent, so _3_ must not be picked up even though it's set.
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "spectrum",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "TRAP_DESTINATION_3_HOST": "smarts",
            "AUTH_TYPE": "NONE",
            "APPNETA_V3_ID_OFFSET": "42",
        })
        self.assertEqual(len(config.trap_destinations), 1)
        self.assertEqual(config.trap_destinations[0].host, "spectrum")

    def test_invalid_scheme_raises(self):
        with self.assertRaises(ConfigError):
            load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_SCHEME": "bogus",
                "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
                "AUTH_TYPE": "NONE",
            })

    def test_missing_scheme_raises(self):
        # No silent default -- see README Step 3: a wrong guess produces a
        # healthy-looking container that never turns traps into alarms.
        with self.assertRaises(ConfigError):
            load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "AUTH_TYPE": "NONE",
            })


class TestSourceAddress(unittest.TestCase):
    def _env(self, **extra):
        env = {
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "AUTH_TYPE": "NONE",
        }
        env.update(extra)
        return env

    def test_source_address_required(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._env())
        self.assertIn("TRAP_DESTINATION_1_SOURCE_ADDRESS", str(ctx.exception))
        self.assertIn("is not set", str(ctx.exception))
        self.assertNotIn("None", str(ctx.exception))

    def test_source_address_valid_ipv4_is_kept(self):
        config = load_config(self._env(TRAP_DESTINATION_1_SOURCE_ADDRESS="192.0.2.147"))
        self.assertEqual(config.trap_destinations[0].source_address, "192.0.2.147")

    def test_source_address_none_is_opt_out_case_and_space_insensitive(self):
        for raw in ("none", "NONE", " None "):
            config = load_config(self._env(TRAP_DESTINATION_1_SOURCE_ADDRESS=raw))
            self.assertEqual(config.trap_destinations[0].source_address, "", raw)

    def test_source_address_invalid_values_rejected(self):
        for raw in ("256.1.1.1", "1.2.3", "01.02.03.04", "::1", "host.example.com", ""):
            with self.assertRaises(ConfigError, msg=raw) as ctx:
                load_config(self._env(TRAP_DESTINATION_1_SOURCE_ADDRESS=raw))
            self.assertIn("TRAP_DESTINATION_1_SOURCE_ADDRESS", str(ctx.exception), raw)

    def test_source_address_is_per_destination(self):
        config = load_config(self._env(
            TRAP_DESTINATION_1_SOURCE_ADDRESS="192.0.2.147",
            TRAP_DESTINATION_2_HOST="smarts",
            TRAP_DESTINATION_2_SCHEME="general",
            TRAP_DESTINATION_2_SOURCE_ADDRESS="none",
        ))
        self.assertEqual(config.trap_destinations[0].source_address, "192.0.2.147")
        self.assertEqual(config.trap_destinations[1].source_address, "")


if __name__ == "__main__":
    unittest.main()


class TestNumericEnvValidation(unittest.TestCase):
    BASE = {
        "TRAP_DESTINATION_1_HOST": "h",
        "TRAP_DESTINATION_1_SCHEME": "general",
        "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
        "AUTH_TYPE": "NONE",
    }

    def test_non_integer_values_raise_config_error(self):
        for key in ("LISTEN_PORT", "TRAP_DESTINATION_1_PORT", "APPNETA_V3_ID_OFFSET"):
            with self.assertRaises(ConfigError, msg=key):
                load_config({**self.BASE, key: "abc"})
