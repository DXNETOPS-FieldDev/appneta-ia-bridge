import base64
import unittest

from alarm_bridge.auth import AuthError, validate_auth


class TestValidateAuthNone(unittest.TestCase):
    def test_none_always_passes(self):
        validate_auth({}, "NONE")  # should not raise
        validate_auth({"Authorization": "garbage"}, "none")  # case-insensitive


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
        raw = f"{username}:{password}".encode("utf-8")
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
