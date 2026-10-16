"""Maps an AppNeta Intelligent Alarms webhook payload to SNMP trap varbinds
under APPNETA-INTELLIGENT-ALARM-MIB (see mibs/APPNETA-INTELLIGENT-ALARM-MIB.mib).

Trap selection is keyed on (alarm-vs-event tier, state) -- two tiers x
Raised/Cleared = four trap OIDs, used for every rule regardless of its name, so
customers can add, rename or delete AppNeta alarm rules without any change here
or in Spectrum. The tier (Alarm for MINOR/MAJOR/CRITICAL, Event for WARNING) is
decided here in Python rather than by Spectrum's EventDisp rule engine, so
alarm creation does not depend on Spectrum-side rule evaluation.
"""

import logging

logger = logging.getLogger("alarm_bridge")

APN_ALARM_BASE = "1.3.6.1.4.1.19041.2.1.5.10"
APN_NOTIF_BASE = "1.3.6.1.4.1.19041.2.1.103.0"

# Maps our internal field names to their MIB object OIDs. Field names here
# are the mapping.py-internal vocabulary; the MIB objects themselves are
# prefixed (alarmDescription, alarmTarget, etc.) to avoid colliding with
# APPNETA-MIB's own object names when compiled together -- see the .mib file.
OID = {
    "alarmId": f"{APN_ALARM_BASE}.1",
    "orgId": f"{APN_ALARM_BASE}.2",
    "alarmType": f"{APN_ALARM_BASE}.3",
    "alarmSeverity": f"{APN_ALARM_BASE}.4",
    "ruleId": f"{APN_ALARM_BASE}.5",
    "rule": f"{APN_ALARM_BASE}.6",
    "description": f"{APN_ALARM_BASE}.7",
    "itemId": f"{APN_ALARM_BASE}.8",
    "itemName": f"{APN_ALARM_BASE}.9",
    "itemType": f"{APN_ALARM_BASE}.10",
    "target": f"{APN_ALARM_BASE}.11",
    "monitoringPointId": f"{APN_ALARM_BASE}.12",
    "monitoringPointName": f"{APN_ALARM_BASE}.13",
    "monitoringPolicyId": f"{APN_ALARM_BASE}.14",
    "monitoringPolicyName": f"{APN_ALARM_BASE}.15",
    "monitoringPolicyGroupId": f"{APN_ALARM_BASE}.16",
    "monitoringPolicyGroupName": f"{APN_ALARM_BASE}.17",
    "eventTimestamp": f"{APN_ALARM_BASE}.18",
    "providerLink": f"{APN_ALARM_BASE}.19",
    "tags": f"{APN_ALARM_BASE}.20",
    "itemId32": f"{APN_ALARM_BASE}.21",
    "monitoringPointId32": f"{APN_ALARM_BASE}.22",
    "alarmSeverityLvl": f"{APN_ALARM_BASE}.23",
    "fieldNineTarget": f"{APN_ALARM_BASE}.24",
}

# Item types with a v4->v3 id conversion (v3_id = v4_id - offset).
_ITEM_TYPES_WITH_V3_ID = frozenset({"NETWORK_PATH", "WEB_PATH"})

# Spectrum's EventDisp resolves alarm severity dynamically via a severity-map
# lookup on this value (SDN_INTEG.IntelligentAlarmSeverity, see
# spectrum-install/SeverityMaps.IntelligentAlarmSeverity), mirroring the
# native AppNeta MIB's own SeverityMaps.ServiceQualityType convention. MINOR(20),
# MAJOR(30) and CRITICAL(40) have entries in that map and raise a real alarm;
# WARNING(10) is deliberately unmapped and produces an event only (see
# _resolve_tier).
SEVERITY_LEVEL = {
    "CRITICAL": 40,
    "MAJOR": 30,
    "MINOR": 20,
    "WARNING": 10,
}


def _resolve_tier(severity_lvl):
    return "Alarm" if severity_lvl >= 20 else "Event"


# Every rule -- AppNeta default or customer-created -- uses these four traps; the
# rule name and description travel as varbinds. OIDs .90-.121 (the former
# per-rule-family traps) are retired and no longer sent.
TRAP_OID_ALARM_RAISED = f"{APN_NOTIF_BASE}.122"
TRAP_OID_ALARM_CLEARED = f"{APN_NOTIF_BASE}.123"
TRAP_OID_EVENT_RAISED = f"{APN_NOTIF_BASE}.124"
TRAP_OID_EVENT_CLEARED = f"{APN_NOTIF_BASE}.125"

TRAP_OID_GENERAL_CRITICAL_RAISED = f"{APN_NOTIF_BASE}.126"
TRAP_OID_GENERAL_CRITICAL_CLEARED = f"{APN_NOTIF_BASE}.127"
TRAP_OID_GENERAL_MAJOR_RAISED = f"{APN_NOTIF_BASE}.128"
TRAP_OID_GENERAL_MAJOR_CLEARED = f"{APN_NOTIF_BASE}.129"
TRAP_OID_GENERAL_MINOR_RAISED = f"{APN_NOTIF_BASE}.130"
TRAP_OID_GENERAL_MINOR_CLEARED = f"{APN_NOTIF_BASE}.131"
TRAP_OID_GENERAL_WARNING_RAISED = f"{APN_NOTIF_BASE}.132"
TRAP_OID_GENERAL_WARNING_CLEARED = f"{APN_NOTIF_BASE}.133"

_TRAP_OID_GENERAL_BY_SEVERITY_AND_STATE = {
    ("CRITICAL", "RAISED"): TRAP_OID_GENERAL_CRITICAL_RAISED,
    ("CRITICAL", "CLEARED"): TRAP_OID_GENERAL_CRITICAL_CLEARED,
    ("MAJOR", "RAISED"): TRAP_OID_GENERAL_MAJOR_RAISED,
    ("MAJOR", "CLEARED"): TRAP_OID_GENERAL_MAJOR_CLEARED,
    ("MINOR", "RAISED"): TRAP_OID_GENERAL_MINOR_RAISED,
    ("MINOR", "CLEARED"): TRAP_OID_GENERAL_MINOR_CLEARED,
    ("WARNING", "RAISED"): TRAP_OID_GENERAL_WARNING_RAISED,
    ("WARNING", "CLEARED"): TRAP_OID_GENERAL_WARNING_CLEARED,
}

_TRAP_OID_BY_TIER_AND_STATE = {
    ("Alarm", "RAISED"): TRAP_OID_ALARM_RAISED,
    ("Alarm", "CLEARED"): TRAP_OID_ALARM_CLEARED,
    ("Event", "RAISED"): TRAP_OID_EVENT_RAISED,
    ("Event", "CLEARED"): TRAP_OID_EVENT_CLEARED,
}

_STRING_FIELDS = (
    "alarmId", "alarmType", "alarmSeverity", "rule", "description", "itemId",
    "itemName", "itemType", "target", "monitoringPointId", "monitoringPointName",
    "monitoringPolicyId", "monitoringPolicyName", "monitoringPolicyGroupId",
    "monitoringPolicyGroupName", "providerLink",
)
_INTEGER_FIELDS = ("orgId", "ruleId")


class AlarmMappingError(ValueError):
    """Raised when an alarm payload cannot be mapped to a trap."""


def flatten_tags(tags):
    if not tags:
        return ""
    try:
        return ";".join(f"{tag['category']}={tag['value']}" for tag in tags)
    except (TypeError, KeyError) as exc:
        raise AlarmMappingError(f"Malformed tags: {exc!r}") from exc


def _to_v3_id(raw_v4_id, offset: int | None):
    """Converts a v4 API id to its v3 API equivalent, or None if raw_v4_id
    isn't a usable number (missing, empty, non-numeric), offset is unset, or
    the id is smaller than the offset -- the BER encoder doesn't support
    negative integers, so a value in that shape isn't a real v4 id under
    this conversion scheme and must not be sent rather than crashing the
    request downstream in snmp_trap.py."""
    if offset is None:
        return None
    try:
        v4_id = int(raw_v4_id)
    except (TypeError, ValueError):
        return None
    v3_id = v4_id - offset
    if v3_id < 0:
        logger.warning(
            "v4 id %r is smaller than the configured offset (%r); computed a negative "
            "v3 id, which can't be sent -- skipping the 32-bit field for this alarm",
            raw_v4_id, offset,
        )
        return None
    return v3_id


def resolve_target_attribute(item_type, item_id, v3_id_offset):
    """Returns the v3-API id to send as Spectrum's Field 9 (Variable ID
    Field 9, Target Attribute) routing value for NETWORK_PATH/WEB_PATH items
    -- both carry this same v3 id in Spectrum attribute 0x673009f -- or None
    for MONITORING_POINT (which has no matching attribute) or anything else. None means the varbind
    is simply omitted, the same convention _to_v3_id's callers already use
    for itemId32/monitoringPointId32."""
    if str(item_type or "").upper() not in _ITEM_TYPES_WITH_V3_ID:
        return None
    return _to_v3_id(item_id, v3_id_offset)


def _resolve_state_and_timestamp(payload):
    if not isinstance(payload, dict):
        raise AlarmMappingError(f"Alarm payload must be a JSON object, got {type(payload).__name__}")
    state = payload.get("state")
    if state == "RAISED":
        return state, payload.get("raisedTime", "")
    if state == "CLEARED":
        return state, payload.get("clearedTime", "")
    raise AlarmMappingError(f"Unknown or missing alarm state: {state!r}")


def _build_common_varbinds(payload, timestamp):
    varbinds = []
    for field in _STRING_FIELDS:
        varbinds.append((OID[field], str(payload.get(field, "")), "string"))
    for field in _INTEGER_FIELDS:
        raw = payload.get(field)
        if raw in (None, ""):
            logger.warning("Alarm payload missing %r; defaulting to 0", field)
            value = 0
        else:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise AlarmMappingError(f"{field} must be an integer, got {raw!r}") from None
            if value < 0:
                raise AlarmMappingError(f"{field} must not be negative, got {raw!r}")
        varbinds.append((OID[field], value, "integer"))
    varbinds.append((OID["eventTimestamp"], timestamp, "string"))
    varbinds.append((OID["tags"], flatten_tags(payload.get("tags")), "string"))
    return varbinds


def map_alarm_to_trap(payload: dict, v3_id_offset: int | None = None):
    state, timestamp = _resolve_state_and_timestamp(payload)

    severity = payload.get("alarmSeverity")
    severity_lvl = SEVERITY_LEVEL.get(severity)
    if severity_lvl is None:
        raise AlarmMappingError(f"Unknown or missing alarm severity: {severity!r}")

    tier = _resolve_tier(severity_lvl)
    trap_oid = _TRAP_OID_BY_TIER_AND_STATE[(tier, state)]

    varbinds = _build_common_varbinds(payload, timestamp)
    varbinds.append((OID["alarmSeverityLvl"], severity_lvl, "integer"))

    if v3_id_offset is not None:
        mp_id_v3 = _to_v3_id(payload.get("monitoringPointId"), v3_id_offset)
        if mp_id_v3 is not None:
            varbinds.append((OID["monitoringPointId32"], mp_id_v3, "integer"))
        if str(payload.get("itemType", "")).upper() in _ITEM_TYPES_WITH_V3_ID:
            item_id_v3 = _to_v3_id(payload.get("itemId"), v3_id_offset)
            if item_id_v3 is not None:
                varbinds.append((OID["itemId32"], item_id_v3, "integer"))

    target_attr = resolve_target_attribute(
        payload.get("itemType"), payload.get("itemId"), v3_id_offset
    )
    if target_attr is not None:
        varbinds.append((OID["fieldNineTarget"], target_attr, "integer"))

    return trap_oid, varbinds


def map_alarm_to_general_trap(payload: dict):
    """Maps an alarm to the shared `general`-mode trap set (Spectrum
    `general` mode and Smarts both consume this) -- severity fixed per OID,
    no dynamic severity resolution, no Field 9 targeting, no 32-bit id
    conversion.
    """
    state, timestamp = _resolve_state_and_timestamp(payload)

    severity = payload.get("alarmSeverity")
    if severity not in SEVERITY_LEVEL:
        raise AlarmMappingError(f"Unknown or missing alarm severity: {severity!r}")

    trap_oid = _TRAP_OID_GENERAL_BY_SEVERITY_AND_STATE[(severity, state)]
    varbinds = _build_common_varbinds(payload, timestamp)
    return trap_oid, varbinds
