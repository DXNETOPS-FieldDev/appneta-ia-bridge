import tempfile
import unittest
from typing import ClassVar
from pathlib import Path

from alarm_bridge.config import ConfigError, TrapDestination, load_config, read_log_level


class TestLoadConfig(unittest.TestCase):
    def test_minimal_valid_env(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "spectrum.example.com",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
        })
        self.assertEqual(config.listen_port, 8080)
        self.assertEqual(config.auth_type, "BEARER")
        self.assertEqual(
            config.trap_destinations,
            [TrapDestination(host="spectrum.example.com", port=162, community="public", scheme="general", slot=1)],
        )

    def test_missing_trap_destination_1_host_raises(self):
        with self.assertRaises(ConfigError):
            load_config({"AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token"})

    def test_missing_auth_type_raises(self):
        # AUTH_TYPE has no default -- an operator must consciously choose a scheme
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
            "TRAP_DESTINATION_1_COMMUNITY": "public",
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
                "TRAP_DESTINATION_1_COMMUNITY": "public",
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
                "TRAP_DESTINATION_1_COMMUNITY": "public",
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
                "TRAP_DESTINATION_1_COMMUNITY": "public",
                "AUTH_TYPE": "BEARER",
                "AUTH_TOKEN_FILE": "/nonexistent/path/token",
            })

    def test_basic_password_read_from_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            password_path = Path(tmpdir) / "password"
            password_path.write_text("hunter2\n")
            config = load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_COMMUNITY": "public",
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
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            "LISTEN_PORT": "9090",
        })
        self.assertEqual(config.listen_port, 9090)

    def _base_env(self, **extra):
        return {
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            **extra,
        }

    def test_listen_host_defaults_to_loopback(self):
        self.assertEqual(load_config(self._base_env()).listen_host, "127.0.0.1")

    def test_listen_host_override_applied(self):
        config = load_config(self._base_env(LISTEN_HOST=" 127.0.0.1 "))
        self.assertEqual(config.listen_host, "127.0.0.1")

    def test_listen_host_rejects_non_ipv4(self):
        for bad in ("localhost", "::1", "", "127.0.0.1; rm", "999.1.1.1"):
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as ctx:
                load_config(self._base_env(LISTEN_HOST=bad))
            self.assertIn("LISTEN_HOST", str(ctx.exception))

    def test_v3_id_offset_defaults_to_none_for_general_scheme(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
        })
        self.assertIsNone(config.v3_id_offset)

    def test_vna_scheme_requires_offset(self):
        # The v4->v3 offset is fixed per AppNeta platform host, not universal
        # (2^41 on one host, 2^40 on another), so there is no safe default.
        # The `vna` scheme sends 32-bit IDs and therefore always requires an offset.
        with self.assertRaises(ConfigError):
            load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_COMMUNITY": "public",
                "TRAP_DESTINATION_1_SCHEME": "vna",
                "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
                "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            })

    def test_vna_scheme_with_offset_succeeds(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            "APPNETA_V3_ID_OFFSET": "2199023255552",
        })
        self.assertEqual(config.v3_id_offset, 2199023255552)

    def test_vna_scheme_with_known_nis_server_succeeds(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            "APPNETA_NIS_SERVER": "app-12",
        })
        self.assertEqual(config.v3_id_offset, 10995116277760)

    def test_vna_scheme_with_unknown_nis_server_raises(self):
        # app-99 isn't a real/surveyed NIS server -- must fail closed, never
        # silently fall back to no offset (a wrong offset mis-correlates alarms).
        with self.assertRaises(ConfigError):
            load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_COMMUNITY": "public",
                "TRAP_DESTINATION_1_SCHEME": "vna",
                "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
                "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
                "APPNETA_NIS_SERVER": "app-99",
            })

    def test_explicit_offset_takes_precedence_over_nis_server(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            "APPNETA_NIS_SERVER": "app-12",
            "APPNETA_V3_ID_OFFSET": "42",
        })
        self.assertEqual(config.v3_id_offset, 42)


class TestTrapDestinations(unittest.TestCase):
    def _community_env(self, **extra):
        return {
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            **extra,
        }

    def test_community_has_no_default(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._community_env())
        self.assertIn("TRAP_DESTINATION_1_COMMUNITY", str(ctx.exception))
        self.assertIn("TRAP_DESTINATION_1_COMMUNITY_FILE", str(ctx.exception))

    def test_empty_community_is_rejected(self):
        with self.assertRaises(ConfigError):
            load_config(self._community_env(TRAP_DESTINATION_1_COMMUNITY=""))

    def test_missing_community_names_the_destination_that_lacks_it(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._community_env(
                TRAP_DESTINATION_1_COMMUNITY="c1",
                TRAP_DESTINATION_2_HOST="t",
                TRAP_DESTINATION_2_SCHEME="general",
                TRAP_DESTINATION_2_SOURCE_ADDRESS="none",
            ))
        self.assertIn("TRAP_DESTINATION_2_COMMUNITY", str(ctx.exception))
        self.assertNotIn("TRAP_DESTINATION_1_COMMUNITY", str(ctx.exception))

    def test_community_read_from_file_and_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "community"
            path.write_text("s3cret-string\n")
            config = load_config(self._community_env(TRAP_DESTINATION_1_COMMUNITY_FILE=str(path)))
        self.assertEqual(config.trap_destinations[0].community, "s3cret-string")

    def test_community_file_takes_precedence_over_value(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "community"
            path.write_text("from-file")
            config = load_config(self._community_env(
                TRAP_DESTINATION_1_COMMUNITY="from-env",
                TRAP_DESTINATION_1_COMMUNITY_FILE=str(path),
            ))
        self.assertEqual(config.trap_destinations[0].community, "from-file")

    def test_unreadable_community_file_raises_config_error(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._community_env(TRAP_DESTINATION_1_COMMUNITY_FILE="/nonexistent/community"))
        self.assertIn("TRAP_DESTINATION_1_COMMUNITY_FILE", str(ctx.exception))

    def test_non_utf8_community_in_env_is_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._community_env(TRAP_DESTINATION_1_COMMUNITY="ab\udcffcd"))
        self.assertIn("TRAP_DESTINATION_1_COMMUNITY must be valid UTF-8", str(ctx.exception))

    def test_non_utf8_community_file_raises_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "community"
            path.write_bytes(b"ab\xffcd")
            with self.assertRaises(ConfigError) as ctx:
                load_config(self._community_env(TRAP_DESTINATION_1_COMMUNITY_FILE=str(path)))
        self.assertIn("TRAP_DESTINATION_1_COMMUNITY_FILE", str(ctx.exception))

    def test_single_destination_defaults(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "spectrum.example.com",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
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
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
        })
        [dest] = config.trap_destinations
        self.assertEqual(dest.port, 1620)
        self.assertEqual(dest.community, "private")
        self.assertEqual(dest.scheme, "general")

    def test_two_destinations_parsed(self):
        config = load_config({
            "TRAP_DESTINATION_1_HOST": "spectrum",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "TRAP_DESTINATION_2_HOST": "smarts",
            "TRAP_DESTINATION_2_COMMUNITY": "public",
            "TRAP_DESTINATION_2_SCHEME": "general",
            "TRAP_DESTINATION_2_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
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
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "vna",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "TRAP_DESTINATION_3_HOST": "smarts",
            "TRAP_DESTINATION_3_COMMUNITY": "public",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            "APPNETA_V3_ID_OFFSET": "42",
        })
        self.assertEqual(len(config.trap_destinations), 1)
        self.assertEqual(config.trap_destinations[0].host, "spectrum")

    def test_invalid_scheme_raises(self):
        with self.assertRaises(ConfigError):
            load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_COMMUNITY": "public",
                "TRAP_DESTINATION_1_SCHEME": "bogus",
                "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
                "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            })

    def test_missing_scheme_raises(self):
        # No silent default -- see README Step 3: a wrong guess produces a
        # healthy-looking container that never turns traps into alarms.
        with self.assertRaises(ConfigError):
            load_config({
                "TRAP_DESTINATION_1_HOST": "s",
                "TRAP_DESTINATION_1_COMMUNITY": "public",
                "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            })


class TestAuthTypeSelection(unittest.TestCase):
    def _env(self, **extra):
        env = {
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
        }
        env.update(extra)
        return env

    def test_none_is_not_a_supported_auth_type(self):
        for raw in ("NONE", "none", " None "):
            with self.assertRaises(ConfigError, msg=raw) as cm:
                load_config(self._env(AUTH_TYPE=raw))
            self.assertIn("no unauthenticated mode", str(cm.exception))

    def test_unknown_auth_type_is_rejected_at_startup(self):
        with self.assertRaises(ConfigError) as cm:
            load_config(self._env(AUTH_TYPE="BEARRER", AUTH_TOKEN="t"))
        self.assertIn("'BEARRER' is not supported", str(cm.exception))

    def test_header_auth_requires_name_and_secret(self):
        with self.assertRaises(ConfigError) as cm:
            load_config(self._env(AUTH_TYPE="HEADER", AUTH_TOKEN="t"))
        self.assertIn("AUTH_HEADER_NAME", str(cm.exception))
        with self.assertRaises(ConfigError) as cm:
            load_config(self._env(AUTH_TYPE="HEADER", AUTH_HEADER_NAME="X-Api-Key"))
        self.assertIn("AUTH_TOKEN", str(cm.exception))

    def test_header_auth_loads_with_valid_name(self):
        config = load_config(self._env(AUTH_TYPE="header", AUTH_HEADER_NAME="X-Api-Key", AUTH_TOKEN="t"))
        self.assertEqual((config.auth_type, config.auth_header_name), ("HEADER", "X-Api-Key"))

    def test_header_name_must_be_a_plain_http_token(self):
        # CRLF/space/colon/underscore would enable header injection or be dropped by nginx.
        for bad in ("X-Api\r\nX-Evil: 1", "X Api", "X:Api", "X_Api_Key", "a" * 65):
            with self.assertRaises(ConfigError, msg=repr(bad)) as cm:
                load_config(self._env(AUTH_TYPE="HEADER", AUTH_HEADER_NAME=bad, AUTH_TOKEN="t"))
            self.assertIn("AUTH_HEADER_NAME", str(cm.exception))


class TestSourceAddress(unittest.TestCase):
    def _env(self, **extra):
        env = {
            "TRAP_DESTINATION_1_HOST": "s",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
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
            TRAP_DESTINATION_2_COMMUNITY="public",
            TRAP_DESTINATION_2_SCHEME="general",
            TRAP_DESTINATION_2_SOURCE_ADDRESS="none",
        ))
        self.assertEqual(config.trap_destinations[0].source_address, "192.0.2.147")
        self.assertEqual(config.trap_destinations[1].source_address, "")


if __name__ == "__main__":
    unittest.main()


class TestNumericEnvValidation(unittest.TestCase):
    BASE: ClassVar[dict] = {
        "TRAP_DESTINATION_1_HOST": "h",
        "TRAP_DESTINATION_1_COMMUNITY": "public",
        "TRAP_DESTINATION_1_SCHEME": "general",
        "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
        "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
    }

    def test_non_integer_values_raise_config_error(self):
        for key in ("LISTEN_PORT", "TRAP_DESTINATION_1_PORT", "APPNETA_V3_ID_OFFSET"):
            with self.assertRaises(ConfigError, msg=key):
                load_config({**self.BASE, key: "abc"})


class TestDedupAndLoggingSettings(unittest.TestCase):
    def _env(self, **extra):
        return {
            "TRAP_DESTINATION_1_HOST": "h",
            "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "general",
            "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
            **extra,
        }

    def test_defaults(self):
        config = load_config(self._env())
        self.assertEqual(config.dedup_state_dir, "")
        self.assertEqual(config.dedup_ttl_hours, 96)
        self.assertEqual(config.dedup_max_entries, 100000)
        self.assertEqual(config.log_level, "INFO")
        self.assertEqual(config.log_syslog_host, "")
        self.assertEqual(config.log_syslog_port, 0)
        self.assertEqual(config.log_syslog_protocol, "udp")

    def test_whitespace_only_dedup_state_dir_is_unset(self):
        self.assertEqual(load_config(self._env(DEDUP_STATE_DIR="  \t ")).dedup_state_dir, "")
        self.assertEqual(load_config(self._env(DEDUP_STATE_DIR=" /var/lib/bridge ")).dedup_state_dir, "/var/lib/bridge")

    def test_valid_overrides_parsed(self):
        config = load_config(self._env(
            DEDUP_STATE_DIR="/var/lib/bridge",
            DEDUP_TTL_HOURS="24",
            DEDUP_MAX_ENTRIES="500",
            LOG_LEVEL="warning",
            LOG_SYSLOG_ADDRESS="collector.example:1514",
            LOG_SYSLOG_PROTOCOL="TCP",
        ))
        self.assertEqual(config.dedup_state_dir, "/var/lib/bridge")
        self.assertEqual(config.dedup_ttl_hours, 24)
        self.assertEqual(config.dedup_max_entries, 500)
        self.assertEqual(config.log_level, "WARNING")
        self.assertEqual(config.log_syslog_host, "collector.example")
        self.assertEqual(config.log_syslog_port, 1514)
        self.assertEqual(config.log_syslog_protocol, "tcp")

    def test_ipv6_syslog_address_brackets_stripped(self):
        config = load_config(self._env(LOG_SYSLOG_ADDRESS="[::1]:514"))
        self.assertEqual((config.log_syslog_host, config.log_syslog_port), ("::1", 514))
        with self.assertRaises(ConfigError):
            load_config(self._env(LOG_SYSLOG_ADDRESS="[]:514"))

    def test_empty_values_mean_default(self):
        config = load_config(self._env(
            DEDUP_TTL_HOURS="", DEDUP_MAX_ENTRIES="", LOG_LEVEL="",
            LOG_SYSLOG_ADDRESS="", LOG_SYSLOG_PROTOCOL="",
        ))
        self.assertEqual((config.dedup_ttl_hours, config.dedup_max_entries), (96, 100000))
        self.assertEqual((config.log_level, config.log_syslog_host, config.log_syslog_protocol),
                         ("INFO", "", "udp"))

    def test_invalid_values_rejected_and_named(self):
        cases = [
            ("DEDUP_TTL_HOURS", "0"), ("DEDUP_TTL_HOURS", "abc"),
            ("DEDUP_MAX_ENTRIES", "99"), ("DEDUP_MAX_ENTRIES", "abc"),
            ("LOG_LEVEL", "LOUD"), ("LOG_SYSLOG_PROTOCOL", "sctp"),
            ("LOG_SYSLOG_ADDRESS", "nocolon"), ("LOG_SYSLOG_ADDRESS", "h:0"),
            ("LOG_SYSLOG_ADDRESS", "h:70000"), ("LOG_SYSLOG_ADDRESS", "h:x"),
            ("LOG_SYSLOG_ADDRESS", ":514"),
        ]
        for key, bad in cases:
            with self.subTest(key=key, bad=bad), self.assertRaises(ConfigError) as ctx:
                load_config(self._env(**{key: bad}))
            self.assertIn(key, str(ctx.exception))

    def test_syslog_address_list_parsed(self):
        config = load_config(self._env(LOG_SYSLOG_ADDRESS=" a.example:514 , [::1]:1514 ,"))
        self.assertEqual(config.log_syslog_targets, [("a.example", 514), ("::1", 1514)])
        self.assertEqual((config.log_syslog_host, config.log_syslog_port), ("a.example", 514))
        self.assertEqual(load_config(self._env()).log_syslog_targets, [])

    def test_bad_syslog_list_entry_names_itself_without_control_characters(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._env(LOG_SYSLOG_ADDRESS="a:514,bad\x1b[31mhost:x"))
        text = str(ctx.exception)
        self.assertIn("LOG_SYSLOG_ADDRESS", text)
        self.assertIn("bad", text)
        self.assertNotIn("a:514", text)
        self.assertNotIn("\x1b", text)

    def test_read_log_level(self):
        self.assertEqual(read_log_level({}), "INFO")
        self.assertEqual(read_log_level({"LOG_LEVEL": "debug"}), "DEBUG")
        with self.assertRaises(ConfigError):
            read_log_level({"LOG_LEVEL": "LOUD"})


class TestSpoolAndDestinationIdentity(unittest.TestCase):
    def _env(self, **extra):
        env = {
            "TRAP_DESTINATION_1_HOST": "a", "TRAP_DESTINATION_1_COMMUNITY": "public",
            "TRAP_DESTINATION_1_SCHEME": "general", "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
            "AUTH_TYPE": "BEARER", "AUTH_TOKEN": "test-token",
        }
        env.update(extra)
        return env

    def _two_dest_env(self, **extra):
        return self._env(
            TRAP_DESTINATION_2_HOST="b", TRAP_DESTINATION_2_COMMUNITY="public",
            TRAP_DESTINATION_2_SCHEME="general", TRAP_DESTINATION_2_SOURCE_ADDRESS="none",
            **extra,
        )

    def test_destination_slots_and_ids(self):
        config = load_config(self._two_dest_env())
        self.assertEqual([d.slot for d in config.trap_destinations], [1, 2])
        self.assertEqual([d.dest_id for d in config.trap_destinations], ["slot1", "slot2"])
        config = load_config(self._two_dest_env(TRAP_DESTINATION_2_NAME="smarts"))
        self.assertEqual([d.dest_id for d in config.trap_destinations], ["slot1", "smarts"])

    def test_slots_assigned_for_cli_destinations(self):
        dests = [TrapDestination("x", 162, "c", "general"), TrapDestination("y", 162, "c", "general")]
        config = load_config(self._env(), trap_destinations=dests)
        self.assertEqual([d.dest_id for d in config.trap_destinations], ["slot1", "slot2"])

    def test_probe_tcp_port_parsed_and_validated(self):
        config = load_config(self._env(TRAP_DESTINATION_1_PROBE_TCP_PORT="14002"))
        self.assertEqual(config.trap_destinations[0].probe_tcp_port, 14002)
        self.assertEqual(load_config(self._env()).trap_destinations[0].probe_tcp_port, 0)
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._env(TRAP_DESTINATION_1_PROBE_TCP_PORT="70000"))
        self.assertIn("TRAP_DESTINATION_1_PROBE_TCP_PORT", str(ctx.exception))

    def test_spool_defaults(self):
        config = load_config(self._env())
        self.assertEqual(
            (config.spool_max_mb, config.spool_free_reserve_mb, config.spool_drain_rate,
             config.probe_interval_seconds, config.up_after_passes, config.reminder_minutes),
            (256, 128, 20, 10, 2, 15),
        )

    def test_spool_bounds_rejected(self):
        cases = [
            ("SPOOL_MAX_MB", "8"), ("SPOOL_FREE_RESERVE_MB", "-1"), ("SPOOL_DRAIN_RATE", "0"),
            ("SPOOL_DRAIN_RATE", "1001"), ("DEST_PROBE_INTERVAL_SECONDS", "1"),
            ("DEST_PROBE_INTERVAL_SECONDS", "301"), ("DEST_UP_AFTER_PASSES", "0"),
            ("DEST_UP_AFTER_PASSES", "11"), ("DEST_DOWN_REMINDER_MINUTES", "0"),
            ("DEST_DOWN_REMINDER_MINUTES", "1441"),
        ]
        for key, bad in cases:
            with self.subTest(key=key, bad=bad), self.assertRaises(ConfigError) as ctx:
                load_config(self._env(**{key: bad}))
            self.assertIn(key, str(ctx.exception))

    def test_spool_enabled_follows_state_dir(self):
        self.assertFalse(load_config(self._env()).spool_enabled)
        self.assertFalse(load_config(self._env(DEDUP_STATE_DIR="")).spool_enabled)
        self.assertTrue(load_config(self._env(DEDUP_STATE_DIR="/var/lib/x")).spool_enabled)

    def _named(self, name, **extra):
        return load_config(self._two_dest_env(TRAP_DESTINATION_2_NAME=name, **extra))

    def test_invalid_names_rejected(self):
        for bad in ["a/b", "a b", "a\nb", "a\x00", "..", ".", "x" * 65, "café"]:
            with self.subTest(bad=bad), self.assertRaises(ConfigError) as ctx:
                self._named(bad)
            self.assertIn("TRAP_DESTINATION_2_NAME", str(ctx.exception))

    def test_newline_name_with_trailing_newline_rejected(self):
        with self.assertRaises(ConfigError):
            self._named("a\nb\n")

    def test_name_whitespace_stripped_and_empty_falls_back(self):
        self.assertEqual(self._named("  smarts \n").trap_destinations[1].dest_id, "smarts")
        self.assertEqual(self._named("   ").trap_destinations[1].dest_id, "slot2")
        self.assertEqual(self._named("x" * 64).trap_destinations[1].dest_id, "x" * 64)

    def test_duplicate_names_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._two_dest_env(
                TRAP_DESTINATION_1_NAME="same", TRAP_DESTINATION_2_NAME="same"))
        self.assertIn("TRAP_DESTINATION_2_NAME", str(ctx.exception))
        self.assertIn("same", str(ctx.exception))

    def test_name_colliding_with_other_slot_default_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._two_dest_env(TRAP_DESTINATION_1_NAME="slot2"))
        self.assertIn("slot2", str(ctx.exception))

    def test_cli_destinations_validated(self):
        dests = [TrapDestination("x", 162, "c", "general", name="dup"),
                 TrapDestination("y", 162, "c", "general", name="dup")]
        with self.assertRaises(ConfigError):
            load_config(self._env(), trap_destinations=dests)
        bad = [TrapDestination("x", 162, "c", "general", name="../etc")]
        with self.assertRaises(ConfigError):
            load_config(self._env(), trap_destinations=bad)
