import base64
import unittest

from alarm_bridge.auth import AuthError, validate_auth


class TestNoUnauthenticatedMode(unittest.TestCase):
    def test_none_and_empty_are_rejected_not_waved_through(self):
        # There is deliberately no "no authentication" mode; an unset/NONE type fails closed.
        for auth_type in ("NONE", "none", "", None):
            for headers in ({}, {"Authorization": "garbage"}):
                with self.assertRaises(AuthError, msg=(auth_type, headers)):
                    validate_auth(headers, auth_type)  # pyright: ignore[reportArgumentType] -- None is the point: unset type must fail closed


class TestValidateAuthHeader(unittest.TestCase):
    def test_correct_header_value_passes(self):
        validate_auth({"X-Api-Key": "s3cret-value"}, "HEADER", header_name="X-Api-Key", token="s3cret-value")

    def test_wrong_or_missing_value_raises(self):
        for headers in ({"X-Api-Key": "wrong"}, {}):
            with self.assertRaises(AuthError):
                validate_auth(headers, "HEADER", header_name="X-Api-Key", token="s3cret-value")

    def test_non_ascii_value_raises_auth_error(self):
        with self.assertRaises(AuthError):
            validate_auth({"X-Api-Key": "caf\u00e9"}, "HEADER", header_name="X-Api-Key", token="s3cret-value")

    def test_unconfigured_header_name_or_empty_secret_never_matches(self):
        with self.assertRaises(AuthError):
            validate_auth({"X-Api-Key": "x"}, "HEADER", header_name="", token="x")
        with self.assertRaises(AuthError):
            validate_auth({"X-Api-Key": ""}, "HEADER", header_name="X-Api-Key", token="")


class TestEmptySecretNeverMatches(unittest.TestCase):
    def test_empty_bearer_token_does_not_accept_bare_bearer(self):
        with self.assertRaises(AuthError):
            validate_auth({"Authorization": "Bearer "}, "BEARER", token="")

    def test_empty_basic_password_is_rejected(self):
        raw = base64.b64encode(b"svc:").decode("ascii")
        with self.assertRaises(AuthError):
            validate_auth({"Authorization": f"Basic {raw}"}, "BASIC", username="svc", password="")


class TestValidateAuthBearer(unittest.TestCase):
    def test_correct_token_passes(self):
        validate_auth({"Authorization": "Bearer secret123"}, "BEARER", token="secret123")

    def test_non_ascii_header_raises_auth_error_not_type_error(self):
        with self.assertRaises(AuthError):
            validate_auth({"Authorization": "Bearer caf\u00e9"}, "BEARER", token="secret123")

    def test_wrong_token_raises(self):
        with self.assertRaises(AuthError):
            validate_auth({"Authorization": "Bearer wrong"}, "BEARER", token="secret123")

    def test_missing_header_raises(self):
        with self.assertRaises(AuthError):
            validate_auth({}, "BEARER", token="secret123")


class TestValidateAuthBasic(unittest.TestCase):
    def _basic_header(self, username, password):
        raw = f"{username}:{password}".encode()
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def test_correct_credentials_pass(self):
        header = self._basic_header("svc", "hunter2")
        validate_auth({"Authorization": header}, "BASIC", username="svc", password="hunter2")

    def test_wrong_password_raises(self):
        header = self._basic_header("svc", "wrong")
        with self.assertRaises(AuthError):
            validate_auth({"Authorization": header}, "BASIC", username="svc", password="hunter2")

    def test_missing_header_raises(self):
        with self.assertRaises(AuthError):
            validate_auth({}, "BASIC", username="svc", password="hunter2")


class TestValidateAuthUnsupportedType(unittest.TestCase):
    def test_unsupported_type_raises(self):
        with self.assertRaises(AuthError):
            validate_auth({"Authorization": "whatever"}, "DIGEST")


if __name__ == "__main__":
    unittest.main()
