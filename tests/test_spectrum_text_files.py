"""Format checks for the Spectrum Prob*/Event* alarm-text files in both bundles.

These tests guard the layout Spectrum requires of the alarm-text files, so a
malformed file is caught before it is installed. The rules encoded here:
  - Prob line 1 IS the alarm title and supports {S N} substitution; the body is
    static SYMPTOMS / PROBABLE CAUSES / RECOMMENDED ACTIONS text.
  - Varbind indices come from the AlertMap fragments: 301 alarmId, 306 ruleName,
    308 itemName, 318 providerLink (launch-back URL).
  - Every Event file carries alarmId and the launch-back URL.
"""

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUNDLES = [ROOT / "spectrum-install", ROOT / "spectrum-install-general"]
SECTIONS = ("SYMPTOMS:", "PROBABLE CAUSES:", "RECOMMENDED ACTIONS:")


def _files(pattern):
    found = sorted(p for bundle in BUNDLES for p in bundle.glob(pattern))
    if not found:
        raise AssertionError(f"no {pattern} files found under {BUNDLES}")
    return found


class TestProbFiles(unittest.TestCase):
    def test_title_line_is_a_substituted_title(self):
        for path in _files("Prob*_en_US"):
            with self.subTest(file=path.name):
                title = path.read_text().splitlines()[0]
                self.assertTrue(title.startswith("AppNeta Intelligent Alarm"), title)
                self.assertIn("{S 306}", title)
                self.assertIn("item {S 308}", title)

    def test_body_has_the_three_static_sections(self):
        for path in _files("Prob*_en_US"):
            with self.subTest(file=path.name):
                lines = path.read_text().splitlines()
                for section in SECTIONS:
                    self.assertIn(section, lines)


class TestEventFiles(unittest.TestCase):
    def test_carries_alarm_id_rule_item_and_launch_url(self):
        for path in _files("Event*_en_US"):
            with self.subTest(file=path.name):
                text = path.read_text()
                for index in (301, 306, 308, 318):
                    self.assertIn(f"{{S {index}}}", text)

    def test_every_cleared_code_has_an_event_format_file(self):
        """Every cleared vna code needs a CsEvFormat file; without one, an alarm
        clear writes "...for which no event format file exists" into Spectrum's
        event log.

        Driven off the EventDisp fragment's `C` rules rather than a hardcoded
        list, so it covers both bundles and any code added later. Raise/clear
        pairing is not inferable from the code's parity, so the A/C rule is the
        authoritative source.
        """
        clear_rule = re.compile(r"^(0x[0-9a-f]+)\s+E\s+\d+\s+C\s", re.MULTILINE)
        checked = 0
        for bundle in BUNDLES:
            # `*.EventDisp` (NetworkPath.EventDisp, WebPath.EventDisp) carries the vna-mode
            # C rules; the shared fragment declares those codes bare. Glob both naming
            # conventions so this guard covers them.
            for fragment in (*bundle.glob("EventDisp*.fragment"), *bundle.glob("*.EventDisp")):
                for code in clear_rule.findall(fragment.read_text()):
                    name = f"Event{int(code, 16):08x}_en_US"
                    with self.subTest(bundle=bundle.name, code=code):
                        self.assertTrue(
                            (bundle / name).is_file(),
                            f"{bundle.name}: {code} is a CLEAR rule in "
                            f"{fragment.name} but {name} does not exist",
                        )
                    checked += 1
        self.assertGreater(
            checked, 0, "no C rules matched -- the test is not exercising anything"
        )

    def test_every_bare_declared_code_has_an_event_format_file(self):
        """General mode's MINOR/WARNING codes are declared bare (event-only, no
        A/C alarm directive). A bare code is still a logged event and still needs
        a CsEvFormat file, or Spectrum logs "...for which no event format file
        exists". The C-rule test above does not cover bare codes, so this one does.
        """
        bare_rule = re.compile(r"^(0x[0-9a-f]+)\s*$", re.MULTILINE)
        checked = 0
        for bundle in BUNDLES:
            for fragment in bundle.glob("EventDisp*.fragment"):
                for code in bare_rule.findall(fragment.read_text()):
                    name = f"Event{int(code, 16):08x}_en_US"
                    with self.subTest(bundle=bundle.name, code=code):
                        self.assertTrue(
                            (bundle / name).is_file(),
                            f"{bundle.name}: {code} is declared bare (event-only) in "
                            f"{fragment.name} but {name} does not exist",
                        )
                    checked += 1
        self.assertGreater(
            checked, 0, "no bare-declared codes matched -- the test is not exercising anything"
        )


class TestModelTypeScopedEventDisp(unittest.TestCase):
    """Alarm-creation (A/C) directives must live in the EventDisp file of the Spectrum
    model type the event is routed to. A rule that exists only in AppNetaAdmin/EventDisp
    never fires on a NetworkPath or WebPath model, and the same rule duplicated in
    AppNetaAdmin raises every alarm twice. NetworkPath and WebPath therefore each carry
    their own A/C directives, and AppNetaAdmin carries none for these families.
    """

    # family -> model type(s) is a fixed property of AppNeta's alarm schema, not
    # derived from any file: ConnectError/Outage/ExtendedLoss/MultipleDnsErrors/
    # DnsLookupError/TestingErrorLocal only ever carry itemType NETWORK_PATH,
    # MultipleWebErrors/WebErrors only ever carry WEB_PATH, and Unknown is the rule-name
    # fallback so it can carry either itemType.
    FAMILY_MODEL_TYPES = {
        "0x6732150": ("NetworkPath",),  # ConnectError
        "0x6732152": ("NetworkPath",),  # Outage
        "0x6732154": ("NetworkPath",),  # ExtendedLoss
        "0x6732156": ("NetworkPath",),  # MultipleDnsErrors
        "0x6732158": ("NetworkPath",),  # DnsLookupError
        "0x673215a": ("NetworkPath",),  # TestingErrorLocal
        "0x673215c": ("WebPath",),      # MultipleWebErrors
        "0x673215e": ("WebPath",),      # WebErrors
        "0x6732180": ("NetworkPath", "WebPath"),  # Unknown
    }

    @staticmethod
    def _cleared_code(raised_code):
        hex_digits = len(raised_code) - len("0x")
        return f"0x{int(raised_code, 16) + 1:0{hex_digits}x}"

    @staticmethod
    def _expected_lines(raised_code):
        cleared_code = TestModelTypeScopedEventDisp._cleared_code(raised_code)
        return (
            f"{raised_code} E 20 A {{ v 322 SDN_INTEG.IntelligentAlarmSeverity }}, "
            f"{raised_code}, 301",
            f"{cleared_code} E 20 C {raised_code}, 301",
        )

    def test_every_family_has_the_correct_ac_directive_on_its_model_type_file(self):
        checked = 0
        for raised_code, model_types in self.FAMILY_MODEL_TYPES.items():
            raised_line, cleared_line = self._expected_lines(raised_code)
            for model_type in model_types:
                model_file = ROOT / "spectrum-install" / f"{model_type}.EventDisp"
                with self.subTest(code=raised_code, model_type=model_type):
                    self.assertTrue(model_file.is_file(), f"missing {model_file}")
                    text = model_file.read_text()
                    self.assertIn(raised_line, text, f"{model_type}.EventDisp missing: {raised_line!r}")
                    self.assertIn(cleared_line, text, f"{model_type}.EventDisp missing: {cleared_line!r}")
                checked += 1
        self.assertGreater(
            checked, 0, "no codes checked -- the test is not exercising anything"
        )

    def test_appnetaadmin_has_no_duplicate_alarm_creation(self):
        """AppNetaAdmin/EventDisp must not carry an A/C directive for any of the 9
        families, since NetworkPath/WebPath each have their own -- if it does, every
        alarm is raised twice."""
        source = ROOT / "spectrum-install" / "EventDisp.appneta-ia.fragment"
        text = source.read_text()
        for raised_code in self.FAMILY_MODEL_TYPES:
            raised_line, cleared_line = self._expected_lines(raised_code)
            with self.subTest(code=raised_code):
                self.assertNotIn(
                    raised_line, text,
                    f"{source.name} still has {raised_code}'s alarm-creation directive -- "
                    "this duplicates the alarm now that its model-type file also has it",
                )
                self.assertNotIn(cleared_line, text)


if __name__ == "__main__":
    unittest.main()


class TestSeverityMap(unittest.TestCase):
    def test_minor_major_critical_are_mapped_to_distinct_spectrum_severities(self):
        # MINOR now raises a real alarm (alarm_bridge._resolve_tier), so it needs an entry
        # here; WARNING stays unmapped on purpose (event only).
        text = (BUNDLES[0] / "SeverityMaps.IntelligentAlarmSeverity").read_text()
        mapping = dict(line.split() for line in text.splitlines() if line.strip())
        self.assertEqual(mapping, {"20": "1", "30": "2", "40": "3"})
