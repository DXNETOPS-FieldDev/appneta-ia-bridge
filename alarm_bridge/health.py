"""Destination reachability probes."""

import logging
import socket
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("alarm_bridge")

REASON_MAX = 120


def udp_refused(host: str, port: int, wait: float = 0.5) -> bool:
    """Send a zero-length datagram; True when the host answers ICMP port-unreachable.

    A timeout (dead host, firewall DROP) is not a refusal and returns False.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(wait)
        try:
            sock.connect((host, port))
            sock.send(b"")
            sock.recv(1)
        except ConnectionRefusedError:
            return True
        except socket.timeout:
            return False
    return False


def tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _clean(text) -> str:
    """One-line, control-char-free, bounded form of an exception text for a log line."""
    out = "".join(c if c.isprintable() else " " for c in str(text))
    return out[:REASON_MAX]


class DestinationHealth:
    """Per-destination up/down state. Healthy destinations are never probed over UDP (no-noise rule)."""

    def __init__(self, destinations, probe_interval, up_after_passes, reminder_seconds,
                 spool_stats_fn, clock=time.monotonic, udp_probe=udp_refused, tcp_probe=tcp_open):
        self._dests = {d.dest_id: d for d in destinations}
        self._interval = probe_interval
        self._up_after = up_after_passes
        self._reminder = reminder_seconds
        self._stats = spool_stats_fn
        self._clock = clock
        self._udp = udp_probe
        self._tcp = tcp_probe
        self._lock = threading.Lock()
        self._state = {i: {"up": True, "since": 0.0, "passes": 0, "last_reminder": 0.0,
                                "base_drained": 0, "base_evicted": 0, "draining": False, "up_at": 0.0}
                       for i in self._dests}
        self.on_up: Optional[Callable[[str], None]] = None  # optional callable(dest_id), run after dest_up is logged
        self._stop = threading.Event()
        self._thread = None

    def is_up(self, dest_id: str) -> bool:
        with self._lock:
            return self._state[dest_id]["up"]

    def _stats_safe(self, dest_id: str) -> dict:
        """spool_stats_fn must never block a state flip; a failure reads as no data (zeros)."""
        try:
            return self._stats(dest_id) or {}
        except Exception:
            return {}

    def mark_down(self, dest_id: str, reason: str) -> None:
        # Snapshot the cumulative counters so dest_drained can report this outage's share only.
        base = self._stats_safe(dest_id)
        with self._lock:
            st = self._state[dest_id]
            if not st["up"]:
                return
            now = self._clock()
            st.update(up=False, since=now, passes=0, last_reminder=now)
            if not st["draining"]:  # down again mid-drain keeps the first baseline: one line per backlog
                st.update(base_drained=base.get("drained", 0), base_evicted=base.get("evicted", 0))
        logger.warning("event=dest_down dest=%s reason=%s", dest_id, _clean(reason))

    def _probe_round(self, dest, udp: bool) -> bool:
        """True when every configured probe passes. An OSError is a failed probe, not a crash."""
        try:
            if udp and self._udp(dest.host, dest.port):
                return False
            if dest.probe_tcp_port and not self._tcp(dest.host, dest.probe_tcp_port):
                return False
        except OSError:
            return False
        return True

    def check_once(self) -> None:
        for dest_id, dest in self._dests.items():
            with self._lock:
                up = self._state[dest_id]["up"]
            if up:
                if dest.probe_tcp_port and not self._probe_round(dest, udp=False):
                    self.mark_down(dest_id, f"tcp probe to port {dest.probe_tcp_port} failed")
                continue
            ok = self._probe_round(dest, udp=True)
            now = self._clock()
            with self._lock:
                st = self._state[dest_id]
                if st["up"]:
                    continue
                st["passes"] = st["passes"] + 1 if ok else 0
                recovered = st["passes"] >= self._up_after
                if recovered:
                    outage = now - st["since"]
                    st.update(up=True, draining=True, up_at=now)
                remind = not recovered and now - st["last_reminder"] >= self._reminder
                if remind:
                    st["last_reminder"] = now
            stats = self._stats_safe(dest_id) if (recovered or remind) else {}
            if recovered:
                logger.info("event=dest_up dest=%s outage_s=%d", dest_id, outage)
                if self.on_up is not None:
                    try:
                        self.on_up(dest_id)
                    except Exception:  # an observer must never stop recovery
                        logger.error("event=on_up_error dest=%s", dest_id)
                if stats.get("depth") == 0:  # nothing to drain; an unknown depth waits for the drainer
                    self._drained(dest_id, stats)
            elif remind:
                logger.warning(
                    "event=dest_still_down dest=%s spooled=%d oldest_age_s=%d",
                    dest_id, stats.get("depth", 0), stats.get("oldest_age", 0),
                )

    def backlog_empty(self, dest_id: str) -> None:
        """Drainer hook: ``dest_id`` is up and its spool is empty. Logs dest_drained once per outage."""
        with self._lock:
            if not self._state[dest_id]["draining"]:
                return
        self._drained(dest_id, self._stats_safe(dest_id))

    def _drained(self, dest_id: str, stats: dict) -> None:
        with self._lock:
            st = self._state[dest_id]
            if not st["draining"] or not st["up"]:  # prober and drainer can both get here; log once
                return
            st["draining"] = False
            drain_s = self._clock() - st["up_at"]
            base_drained, base_evicted = st["base_drained"], st["base_evicted"]
        logger.info(
            "event=dest_drained dest=%s drained=%d evicted=%d drain_s=%d",
            dest_id,
            max(0, stats.get("drained", 0) - base_drained),
            max(0, stats.get("evicted", 0) - base_evicted),
            drain_s,
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:  # the prober must never die; log and keep going
                logger.error("event=health_probe_error error=%s: %s", type(exc).__name__, _clean(exc))
            self._stop.wait(self._interval)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="dest-health", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
