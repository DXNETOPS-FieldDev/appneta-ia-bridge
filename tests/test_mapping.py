import json
import pathlib
import unittest

from alarm_bridge.mapping import (
    OID,
    SEVERITY_LEVEL,
    TRAP_OID_ALARM_RAISED,
    TRAP_OID_ALARM_CLEARED,
    TRAP_OID_EVENT_RAISED,
    TRAP_OID_EVENT_CLEARED,
    TRAP_OID_GENERAL_CRITICAL_RAISED,
    TRAP_OID_GENERAL_MAJOR_CLEARED,
    TRAP_OID_GENERAL_MINOR_RAISED,
    TRAP_OID_GENERAL_WARNING_RAISED,
    AlarmMappingError,
    map_alarm_to_trap,
    map_alarm_to_general_trap,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_fixture(name):
    return json.loads((FIXTURES / name).read_text())


class TestMapAlarmToTrap(unittest.TestCase):
    def test_raised_alarm_selects_alarm_raised_trap(self):
        payload = load_fixture("alarm_raised.json")
        trap_oid, _ = map_alarm_to_trap(payload)
        self.assertEqual(trap_oid, TRAP_OID_ALARM_RAISED)

    def test_cleared_alarm_selects_alarm_cleared_trap(self):
        payload = load_fixture("alarm_cleared.json")
        trap_oid, _ = map_alarm_to_trap(payload)
        self.assertEqual(trap_oid, TRAP_OID_ALARM_CLEARED)

    def test_trap_does_not_depend_on_rule_name(self):
        # Default rules, customer-renamed rules, brand new rules and a missing rule
        # all take the same trap -- nothing is classified by name.
        payload = load_fixture("alarm_raised.json")
        for rule in ("Outage (Default)", "Testing Error - Connect",
                     "Some Brand New Policy Nobody Has Seen", ""):
            payload["rule"] = rule
            trap_oid, _ = map_alarm_to_trap(payload)
            self.assertEqual(trap_oid, TRAP_OID_ALARM_RAISED, rule)
        del payload["rule"]
        trap_oid, _ = map_alarm_to_trap(payload)
        self.assertEqual(trap_oid, TRAP_OID_ALARM_RAISED)

    def test_rule_name_and_description_travel_as_varbinds(self):
        payload = load_fixture("alarm_raised.json")
        payload["rule"] = "Customer Rule"
        payload["description"] = "Latency greater than 200 ms"
        _, varbinds = map_alarm_to_trap(payload)
        values = {oid: value for oid, value, _ in varbinds}
        self.assertEqual(values[OID["rule"]], "Customer Rule")
        self.assertEqual(values[OID["description"]], "Latency greater than 200 ms")

    def test_minor_major_and_critical_use_alarm_tier(self):
        payload = load_fixture("alarm_raised.json")
        for severity in ("MINOR", "MAJOR", "CRITICAL"):
            payload["alarmSeverity"] = severity
            trap_oid, _ = map_alarm_to_trap(payload)
            self.assertEqual(trap_oid, TRAP_OID_ALARM_RAISED, severity)

    def test_warning_uses_event_tier(self):
        payload = load_fixture("alarm_raised.json")
        payload["alarmSeverity"] = "WARNING"
        trap_oid, _ = map_alarm_to_trap(payload)
        self.assertEqual(trap_oid, TRAP_OID_EVENT_RAISED)
        payload["state"] = "CLEARED"
        trap_oid, _ = map_alarm_to_trap(payload)
        self.assertEqual(trap_oid, TRAP_OID_EVENT_CLEARED)

    def test_unknown_state_raises(self):
        payload = load_fixture("alarm_raised.json")
        payload["state"] = "WEIRD"
        with self.assertRaises(AlarmMappingError):
            map_alarm_to_trap(payload)

    def test_varbinds_contain_alarm_id_as_string(self):
        payload = load_fixture("alarm_raised.json")
        _, varbinds = map_alarm_to_trap(payload)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertEqual(
            as_dict[OID["alarmId"]],
            ("51a81650-4105-4e61-acc6-4e4f4fea0b9c", "string"),
        )

    def test_org_id_mapped_as_integer(self):
        payload = load_fixture("alarm_raised.json")
        _, varbinds = map_alarm_to_trap(payload)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertEqual(as_dict[OID["orgId"]], (18223, "integer"))

    def test_raised_uses_raised_time_as_event_timestamp(self):
        payload = load_fixture("alarm_raised.json")
        _, varbinds = map_alarm_to_trap(payload)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertEqual(
            as_dict[OID["eventTimestamp"]],
            ("2026-08-21T17:46:33.051+0000", "string"),
        )

    def test_cleared_uses_cleared_time_as_event_timestamp(self):
        payload = load_fixture("alarm_cleared.json")
        _, varbinds = map_alarm_to_trap(payload)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertEqual(
            as_dict[OID["eventTimestamp"]],
            ("2026-08-21T17:48:33.794+0000", "string"),
        )

    def test_tags_flattened_to_delimited_string(self):
        payload = load_fixture("alarm_raised.json")
        _, varbinds = map_alarm_to_trap(payload)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        tags_value, tags_type = as_dict[OID["tags"]]
        self.assertEqual(tags_type, "string")
        self.assertEqual(
            tags_value,
            "Target Location=Los Angeles;MP Type=c50;Location=Helsinki Finland",
        )

    def test_missing_tags_maps_to_empty_string(self):
        payload = load_fixture("alarm_raised.json")
        del payload["tags"]
        _, varbinds = map_alarm_to_trap(payload)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertEqual(as_dict[OID["tags"]], ("", "string"))

    def test_all_varbind_oids_are_under_apn_alarm_base_except_org_id(self):
        # orgId is APPNETA-MIB::orgId, shared with native AppNeta traps.
        payload = load_fixture("alarm_raised.json")
        _, varbinds = map_alarm_to_trap(payload)
        for oid, _, _ in varbinds:
            if oid == "1.3.6.1.4.1.19041.2.1.5.1.5":
                continue
            self.assertTrue(oid.startswith("1.3.6.1.4.1.19041.2.1.5.10."))

    def test_org_id_uses_appneta_mib_oid(self):
        self.assertEqual(OID["orgId"], "1.3.6.1.4.1.19041.2.1.5.1.5")

    def test_produces_exactly_twenty_one_varbinds_by_default(self):
        # One per OID entry (alarmId..alarmSeverityLvl) -- a field silently
        # added to or dropped from OID/_STRING_FIELDS/_INTEGER_FIELDS without
        # updating AlertMap.appneta-ia.fragment's matching varbind layout
        # would otherwise go unnoticed here. itemId32/monitoringPointId32
        # (32BIT mode) are conditional, not in this baseline count.
        # NOTE: fieldNineTarget is conditional (only when v3_id_offset is set
        # and itemType is NETWORK_PATH or WEB_PATH), so it's not included in
        # this baseline count either. len(OID) is 24.
        payload = load_fixture("alarm_raised.json")
        _, varbinds = map_alarm_to_trap(payload)
        self.assertEqual(len(varbinds), 21)
        self.assertEqual(len(OID), 24)

    def test_alarm_severity_lvl_varbind_for_each_known_severity(self):
        payload = load_fixture("alarm_raised.json")
        for severity, expected_level in (
            ("CRITICAL", 40), ("MAJOR", 30), ("MINOR", 20), ("WARNING", 10),
        ):
            payload["alarmSeverity"] = severity
            _, varbinds = map_alarm_to_trap(payload)
            as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
            self.assertEqual(as_dict[OID["alarmSeverityLvl"]], (expected_level, "integer"))
            self.assertEqual(SEVERITY_LEVEL[severity], expected_level)

    def test_unrecognized_severity_still_raises(self):
        payload = load_fixture("alarm_raised.json")
        payload["alarmSeverity"] = "WEIRD"
        with self.assertRaises(AlarmMappingError):
            map_alarm_to_trap(payload)

    def test_missing_severity_raises(self):
        payload = load_fixture("alarm_raised.json")
        del payload["alarmSeverity"]
        with self.assertRaises(AlarmMappingError):
            map_alarm_to_trap(payload)

    def test_original_alarm_severity_string_varbind_unchanged(self):
        # alarmSeverity (DisplayString) must survive untouched -- Smarts and
        # any other consumer still needs the original string value.
        payload = load_fixture("alarm_raised.json")
        payload["alarmSeverity"] = "WARNING"
        _, varbinds = map_alarm_to_trap(payload)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertEqual(as_dict[OID["alarmSeverity"]], ("WARNING", "string"))

    def test_missing_org_id_defaults_to_zero_and_logs_warning(self):
        payload = load_fixture("alarm_raised.json")
        del payload["orgId"]
        with self.assertLogs("alarm_bridge", level="WARNING") as logs:
            _, varbinds = map_alarm_to_trap(payload)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertEqual(as_dict[OID["orgId"]], (0, "integer"))
        self.assertTrue(any("orgId" in message for message in logs.output))

    def test_missing_rule_id_defaults_to_zero_and_logs_warning(self):
        payload = load_fixture("alarm_raised.json")
        del payload["ruleId"]
        with self.assertLogs("alarm_bridge", level="WARNING") as logs:
            _, varbinds = map_alarm_to_trap(payload)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertEqual(as_dict[OID["ruleId"]], (0, "integer"))
        self.assertTrue(any("ruleId" in message for message in logs.output))


class TestV3IdConversion(unittest.TestCase):
    """Whenever a v3_id_offset is configured, alongside (not instead of) the
    64-bit v4 IDs AppNeta sends, also compute and include their 32-bit
    v3-API equivalents -- v3_id = v4_id - offset -- so Spectrum can
    correlate against topology already modeled via AppNeta's older, 32-bit
    VNA integration. The offset is fixed per AppNeta platform host (2^41 for
    app-01.pm.appneta.com), not a universal constant. The VNA integration
    only understands 32-bit IDs, so this is gated on `v3_id_offset` alone -- there is no separate 64-bit/
    32-bit mode selector.
    """

    OFFSET = 2199023255552  # 2^41

    def test_no_offset_omits_32bit_fields(self):
        payload = load_fixture("alarm_raised.json")
        _, varbinds = map_alarm_to_trap(payload)
        oids = {oid for oid, _, _ in varbinds}
        self.assertNotIn(OID["itemId32"], oids)
        self.assertNotIn(OID["monitoringPointId32"], oids)

    def test_offset_present_adds_both_fields_for_network_path(self):
        payload = load_fixture("alarm_raised.json")  # itemType NETWORK_PATH
        _, varbinds = map_alarm_to_trap(payload, v3_id_offset=self.OFFSET)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        # fixture itemId=2199023341939, monitoringPointId=2199023290416
        self.assertEqual(as_dict[OID["itemId32"]], (86387, "integer"))
        self.assertEqual(as_dict[OID["monitoringPointId32"]], (34864, "integer"))
        # fieldNineTarget (Field 9) is added for NETWORK_PATH when v3_id_offset is set
        self.assertEqual(as_dict[OID["fieldNineTarget"]], (86387, "integer"))
        self.assertEqual(len(varbinds), 24)

    def test_offset_present_adds_both_fields_for_web_path(self):
        # WEB_PATH uses the same v3 conversion offset as NETWORK_PATH.
        payload = load_fixture("alarm_raised.json")
        payload["itemType"] = "WEB_PATH"
        _, varbinds = map_alarm_to_trap(payload, v3_id_offset=self.OFFSET)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertEqual(as_dict[OID["itemId32"]], (86387, "integer"))
        self.assertEqual(as_dict[OID["monitoringPointId32"]], (34864, "integer"))

    def test_offset_present_omits_item_id32_for_non_network_or_web_path(self):
        # No confirmed v3 conversion formula exists for other item types --
        # sending a guessed value would be worse than sending nothing.
        payload = load_fixture("alarm_raised.json")
        payload["itemType"] = "MONITORING_POINT"
        _, varbinds = map_alarm_to_trap(payload, v3_id_offset=self.OFFSET)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertNotIn(OID["itemId32"], as_dict)
        # monitoringPointId32 isn't item-type-specific -- still included.
        self.assertEqual(as_dict[OID["monitoringPointId32"]], (34864, "integer"))

    def test_offset_present_skips_non_numeric_item_id_gracefully(self):
        payload = load_fixture("alarm_raised.json")
        payload["itemId"] = ""
        _, varbinds = map_alarm_to_trap(payload, v3_id_offset=self.OFFSET)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertNotIn(OID["itemId32"], as_dict)

    def test_offset_present_skips_id_smaller_than_offset_gracefully(self):
        # A v4 id smaller than the offset (e.g. test/placeholder data, or a
        # genuinely unexpected payload) would compute a negative "v3 id" --
        # the BER encoder correctly rejects negative integers, so sending one
        # would crash the request instead of just omitting the field. Found
        # live in production: a smoke-test payload used monitoringPointId="1".
        payload = load_fixture("alarm_raised.json")
        payload["monitoringPointId"] = "1"
        payload["itemId"] = "1"
        with self.assertLogs("alarm_bridge", level="WARNING") as logs:
            _, varbinds = map_alarm_to_trap(payload, v3_id_offset=self.OFFSET)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertNotIn(OID["itemId32"], as_dict)
        self.assertNotIn(OID["monitoringPointId32"], as_dict)
        self.assertTrue(any("negative" in message for message in logs.output))


class TestFieldNineTargetAttribute(unittest.TestCase):
    """Field 9 (Variable ID Field 9, Target Attribute) is Spectrum's routing
    key for redirecting an alarm onto a specific NetworkPath/WebPath model
    instead of the generic AppNetaAdmin device.
    The varbind is always built so the redirect can be enabled in Spectrum
    without any change here.
    """

    OFFSET = 2199023255552  # 2^41, same constant as TestV3IdConversion

    def test_resolves_for_network_path(self):
        from alarm_bridge.mapping import resolve_target_attribute
        result = resolve_target_attribute("NETWORK_PATH", "2199023341939", self.OFFSET)
        self.assertEqual(result, 86387)

    def test_resolves_for_web_path(self):
        from alarm_bridge.mapping import resolve_target_attribute
        result = resolve_target_attribute("WEB_PATH", "2199023341939", self.OFFSET)
        self.assertEqual(result, 86387)

    def test_returns_none_for_monitoring_point(self):
        # MonitoringPoint models have no matching Spectrum attribute, so no
        # Field 9 target is produced for them.
        from alarm_bridge.mapping import resolve_target_attribute
        result = resolve_target_attribute("MONITORING_POINT", "2199023341939", self.OFFSET)
        self.assertIsNone(result)

    def test_returns_none_without_offset(self):
        from alarm_bridge.mapping import resolve_target_attribute
        result = resolve_target_attribute("NETWORK_PATH", "2199023341939", None)
        self.assertIsNone(result)

    def test_varbind_present_in_trap_for_network_path(self):
        payload = load_fixture("alarm_raised.json")  # itemType NETWORK_PATH
        _, varbinds = map_alarm_to_trap(payload, v3_id_offset=self.OFFSET)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertEqual(as_dict[OID["fieldNineTarget"]], (86387, "integer"))

    def test_varbind_absent_for_monitoring_point(self):
        payload = load_fixture("alarm_raised.json")
        payload["itemType"] = "MONITORING_POINT"
        _, varbinds = map_alarm_to_trap(payload, v3_id_offset=self.OFFSET)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertNotIn(OID["fieldNineTarget"], as_dict)

    def test_varbind_absent_without_offset_configured(self):
        # No v3_id_offset at all (the common case for `general` scheme, which
        # never sets APPNETA_NIS_SERVER/APPNETA_V3_ID_OFFSET) -- must not
        # crash or send a bogus value.
        payload = load_fixture("alarm_raised.json")
        _, varbinds = map_alarm_to_trap(payload)
        as_dict = {oid: (value, vtype) for oid, value, vtype in varbinds}
        self.assertNotIn(OID["fieldNineTarget"], as_dict)


class TestMapAlarmToGeneralTrap(unittest.TestCase):
    def test_raised_critical_selects_critical_raised_oid(self):
        payload = load_fixture("alarm_raised.json")
        payload["alarmSeverity"] = "CRITICAL"
        trap_oid, _ = map_alarm_to_general_trap(payload)
        self.assertEqual(trap_oid, TRAP_OID_GENERAL_CRITICAL_RAISED)

    def test_cleared_major_selects_major_cleared_oid(self):
        payload = load_fixture("alarm_cleared.json")
        payload["alarmSeverity"] = "MAJOR"
        trap_oid, _ = map_alarm_to_general_trap(payload)
        self.assertEqual(trap_oid, TRAP_OID_GENERAL_MAJOR_CLEARED)

    def test_minor_and_warning_get_their_own_raised_oids(self):
        payload = load_fixture("alarm_raised.json")
        payload["alarmSeverity"] = "MINOR"
        trap_oid, _ = map_alarm_to_general_trap(payload)
        self.assertEqual(trap_oid, TRAP_OID_GENERAL_MINOR_RAISED)
        payload["alarmSeverity"] = "WARNING"
        trap_oid, _ = map_alarm_to_general_trap(payload)
        self.assertEqual(trap_oid, TRAP_OID_GENERAL_WARNING_RAISED)

    def test_unknown_severity_raises(self):
        payload = load_fixture("alarm_raised.json")
        payload["alarmSeverity"] = "BOGUS"
        with self.assertRaises(AlarmMappingError):
            map_alarm_to_general_trap(payload)

    def test_missing_state_raises(self):
        payload = load_fixture("alarm_raised.json")
        payload["alarmSeverity"] = "MAJOR"
        del payload["state"]
        with self.assertRaises(AlarmMappingError):
            map_alarm_to_general_trap(payload)

    def test_provider_link_present_in_varbinds(self):
        payload = load_fixture("alarm_raised.json")
        payload["alarmSeverity"] = "MAJOR"
        _, varbinds = map_alarm_to_general_trap(payload)
        provider_link = next(v for v in varbinds if v[0] == OID["providerLink"])
        self.assertEqual(provider_link[1], payload["providerLink"])

    def test_no_severity_lvl_or_field_nine_target_varbinds(self):
        # general never resolves severity dynamically or targets a specific
        # model -- these varbinds must never appear, unlike vna's traps.
        payload = load_fixture("alarm_raised.json")
        payload["alarmSeverity"] = "MAJOR"
        _, varbinds = map_alarm_to_general_trap(payload)
        varbind_oids = [v[0] for v in varbinds]
        self.assertNotIn(OID["alarmSeverityLvl"], varbind_oids)
        self.assertNotIn(OID["fieldNineTarget"], varbind_oids)
        self.assertNotIn(OID["itemId32"], varbind_oids)
        self.assertNotIn(OID["monitoringPointId32"], varbind_oids)


if __name__ == "__main__":
    unittest.main()


class TestMalformedPayloads(unittest.TestCase):
    def test_non_object_payload_raises_mapping_error(self):
        from alarm_bridge.mapping import AlarmMappingError, map_alarm_to_general_trap
        for bad in ([], None, "x", 5):
            with self.assertRaises(AlarmMappingError):
                map_alarm_to_trap(bad)
            with self.assertRaises(AlarmMappingError):
                map_alarm_to_general_trap(bad)

    def test_non_numeric_or_negative_org_id_raises_mapping_error(self):
        from alarm_bridge.mapping import AlarmMappingError
        base = {"state": "RAISED", "alarmSeverity": "MAJOR"}
        for bad in ("abc", -1, [1]):
            with self.assertRaises(AlarmMappingError):
                map_alarm_to_trap({**base, "orgId": bad})

    def test_malformed_tags_raise_mapping_error(self):
        from alarm_bridge.mapping import AlarmMappingError
        with self.assertRaises(AlarmMappingError):
            map_alarm_to_trap({"state": "RAISED", "alarmSeverity": "MAJOR", "tags": [{"category": "x"}]})
