import json
import pathlib
import unittest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


class TestFixtures(unittest.TestCase):
    def test_alarm_raised_fixture_loads(self):
        payload = json.loads((FIXTURES / "alarm_raised.json").read_text())
        self.assertEqual(payload["state"], "RAISED")
        self.assertEqual(payload["alarmId"], "51a81650-4105-4e61-acc6-4e4f4fea0b9c")

    def test_alarm_cleared_fixture_loads(self):
        payload = json.loads((FIXTURES / "alarm_cleared.json").read_text())
        self.assertEqual(payload["state"], "CLEARED")
        self.assertIn("clearedTime", payload)
