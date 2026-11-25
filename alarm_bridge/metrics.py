"""Prometheus text exposition (format 0.0.4) of the counters and gauges ``Stats.metrics()`` returns."""

PREFIX = "appneta_ia_bridge_"

_COUNTERS = (
    ("received", "webhooks_received_total", "Webhook POSTs received."),
    ("delivered", "webhooks_delivered_total", "Webhooks answered 200 after a send or a spool write."),
    ("duplicates", "webhooks_duplicates_total", "Webhooks suppressed as duplicates."),
    ("undedupable", "webhooks_undedupable_total", "Webhooks that could not be deduplicated."),
    ("rejected", "webhooks_rejected_total", "Webhooks rejected as malformed or oversized."),
    ("auth_failed", "auth_failures_total", "Requests refused for bad credentials."),
    ("send_failed", "trap_send_failures_total", "Failed trap sends, all destinations."),
    ("inflight_timeouts", "inflight_timeouts_total", "Copies that timed out waiting on a same-key request."),
)


def _escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _num(value):
    return repr(float(value)) if isinstance(value, float) and not value.is_integer() else str(int(value))


def render(snapshot):
    """Return the exposition text for one ``Stats.metrics()`` snapshot."""
    lines = []

    def family(name, kind, help_text, samples):
        full = PREFIX + name
        lines.append(f"# HELP {full} {help_text}")
        lines.append(f"# TYPE {full} {kind}")
        for labels, value in samples:
            if value is None:
                continue  # an observer failed: absent data, not a made-up zero
            label_text = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
            lines.append(f"{full}{{{label_text}}} {_num(value)}" if label_text else f"{full} {_num(value)}")

    for key, name, help_text in _COUNTERS:
        family(name, "counter", help_text, [((), snapshot["counters"][key])])
    family("metrics_auth_failures_total", "counter", "Requests to /metrics refused for bad credentials.",
           [((), snapshot["metrics_auth_failed"])])
    dests = snapshot["dests"]
    family("trap_sends_total", "counter", "Trap send attempts by destination and result.",
           [((("destination", d), ("result", r)), v[r]) for d, v in dests.items() for r in ("ok", "failed")])
    mode = snapshot["dedup_store"]
    family("dedup_store_mode", "gauge", "Dedup store mode (value 1 on the current mode).",
           [((("mode", mode),), 1)] if mode is not None else [])
    if snapshot["spool_enabled"]:
        by_dest = [
            ("destination_up", "gauge", "1 when the destination is up, 0 when down.", lambda v: None if v["up"] is None else int(v["up"])),
            ("spool_depth", "gauge", "Alarms queued for the destination.", lambda v: v["depth"]),
            ("spool_oldest_age_seconds", "gauge", "Age of the oldest queued alarm.", lambda v: v["oldest_age"]),
            ("spool_evicted_total", "counter", "Queued alarms evicted at the spool cap.", lambda v: v["evicted"]),
            ("spool_drained_total", "counter", "Queued alarms delivered after a recovery.", lambda v: v["drained"]),
        ]
        for name, kind, help_text, pick in by_dest:
            family(name, kind, help_text, [((("destination", d),), pick(v)) for d, v in dests.items()])
        family("spool_bytes", "gauge", "Bytes the spool holds on disk.", [((), snapshot["spool_bytes"])])
        family("spool_errors_total", "counter", "Spool failures since start.", [((), snapshot["spool_errors"])])
    return "\n".join(lines) + "\n"
