"""Bounded-volume observability: counters, a periodic summary and destination state changes.

Log volume is O(1) per window plus one line per destination state change. Nothing here logs
per request, attaches a traceback, or includes payload fields or secrets.
"""

import contextlib
import logging
import logging.handlers
import queue
import socket
import sys
import threading
from typing import Callable
import time

logger = logging.getLogger("alarm_bridge")

MAX_REASON_LEN = 200


def _clean(text):
    """One line, bounded: collapse CR/LF/tabs and space runs so text cannot forge log lines."""
    return " ".join(str(text).split())[:MAX_REASON_LEN]
class RateLimitedEvent:
    """``event=<name> dest= count= reason=`` per destination: the first at once, then at most once per
    interval, counting the occurrences the line covers (held-back ones ride on the next line)."""

    def __init__(self, name, interval=60, clock=time.monotonic, timer: Callable = threading.Timer):
        self._name = name
        self._interval = interval
        self._clock = clock
        self._timer = timer
        self._lock = threading.Lock()
        self._last = {}
        self._pending = {}
        self._total = {}
        self._timers = {}  # dest_id -> one-shot timer that reports held-back counts at the window's end

    def total(self, dest_id) -> int:
        with self._lock:
            return self._total.get(dest_id, 0)

    def note(self, dest_id, reason) -> None:
        now = self._clock()
        with self._lock:
            self._total[dest_id] = self._total.get(dest_id, 0) + 1
            self._pending[dest_id] = self._pending.get(dest_id, 0) + 1
            last = self._last.get(dest_id)
            if last is not None and now - last < self._interval:
                if dest_id not in self._timers:
                    t = self._timers[dest_id] = self._timer(
                        self._interval - (now - last), self._flush, (dest_id, reason))
                    t.daemon = True
                    t.start()
                return
            count, self._pending[dest_id], self._last[dest_id] = self._pending[dest_id], 0, now
        logger.warning("event=%s dest=%s count=%d reason=%s", self._name, dest_id, count, _clean(reason))

    def _flush(self, dest_id, reason) -> None:
        """Report what the rate limit held back, so a burst that ends is not left unreported."""
        with self._lock:
            self._timers.pop(dest_id, None)
            count, self._pending[dest_id], self._last[dest_id] = self._pending.get(dest_id, 0), 0, self._clock()
        if count:
            logger.warning("event=%s dest=%s count=%d reason=%s", self._name, dest_id, count, _clean(reason))


_OBSERVER_ERRORS = RateLimitedEvent("observer_error")

STORM_MIN_RECEIVED = 20
STORM_PCT = 50

_COUNTERS = (
    "received", "delivered", "duplicates", "undedupable", "rejected", "auth_failed", "send_failed",
    "inflight_timeouts",
)


def _short_reason(reason, limit=100):
    """One line, bounded: a reject reason can echo a field of the rejected payload."""
    text = " ".join(str(reason).split())
    return text if len(text) <= limit else text[:limit] + "..."


class Stats:
    def __init__(self, destinations, store_mode_fn, clock=time.monotonic, spool_fn=None, state_fn=None):
        self._spool_fn = spool_fn
        self._state_fn = state_fn
        self._store_mode_fn = store_mode_fn
        self._clock = clock
        self._lock = threading.Lock()
        self._counts = dict.fromkeys(_COUNTERS, 0)
        self._totals = dict.fromkeys(_COUNTERS, 0)  # never reset: the summary zeroes _counts every window
        self._dest_totals = {}
        self._first_reject = None  # reason of the first rejected payload in the current window
        self._metrics_auth_failed = 0  # /metrics logins only; kept out of the webhook's auth_failed
        self._dest_order = list(destinations)
        self._dest = {d: self._new_dest() for d in self._dest_order}
        self._window_start = clock()
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def _new_dest():
        return {"ok": 0, "failed": 0, "last": "ok", "failing": False}

    def _bump(self, name):
        with self._lock:
            self._counts[name] += 1
            self._totals[name] += 1

    def received(self):
        self._bump("received")

    def delivered(self):
        self._bump("delivered")

    def duplicate(self):
        self._bump("duplicates")

    def undedupable(self):
        self._bump("undedupable")

    def rejected(self, reason=None):
        with self._lock:  # one acquisition: the count and its reason must land in the same window
            self._counts["rejected"] += 1
            self._totals["rejected"] += 1
            if reason is not None and self._first_reject is None:
                self._first_reject = _short_reason(reason)

    def auth_failed(self):
        self._bump("auth_failed")

    def inflight_timeout(self):
        self._bump("inflight_timeouts")

    def send_ok(self, dest):
        with self._lock:
            d = self._dest.setdefault(dest, self._new_dest())
            d["ok"] += 1
            self._dest_totals.setdefault(dest, {"ok": 0, "failed": 0})["ok"] += 1
        self.recovered(dest)

    def recovered(self, dest):
        """Clear a failing destination (a good send, or health marking it up); logs the OK line once."""
        with self._lock:
            d = self._dest.setdefault(dest, self._new_dest())
            was_failing, d["failing"], d["last"] = d["failing"], False, "ok"
        if was_failing:
            logger.info("event=dest_state dest=%s state=OK", dest)

    def send_failed(self, dest, reason):
        reason = _clean(reason)
        first = False
        with self._lock:
            self._counts["send_failed"] += 1
            self._totals["send_failed"] += 1
            d = self._dest.setdefault(dest, self._new_dest())
            d["failed"] += 1
            self._dest_totals.setdefault(dest, {"ok": 0, "failed": 0})["failed"] += 1
            d["last"] = reason
            if not d["failing"]:
                d["failing"] = True
                first = True
        if first:
            logger.warning("event=dest_state dest=%s state=FAILING reason=%s", dest, reason)

    @staticmethod
    def _call(fn, arg):
        """Optional observers must never break the summary: a failure reads as no data."""
        try:
            return fn(arg)
        except Exception as exc:
            _OBSERVER_ERRORS.note(arg, f"{getattr(fn, '__name__', 'observer')}: {type(exc).__name__}")
            return None

    def _dest_extra(self, name):
        extra = ""
        if self._state_fn is not None:
            up = self._call(self._state_fn, name)
            extra += " state=" + ("down" if up is False else "up")
        if self._spool_fn is not None:
            sp = self._call(self._spool_fn, name) or {}
            extra += (
                f" spooled={sp.get('depth', 0)} oldest_age_s={round(sp.get('oldest_age', 0))}"
                f" evicted={sp.get('evicted', 0)} drained={sp.get('drained', 0)}"
            )
        return extra

    def _needs_attention(self, name, d):
        """A failing, down or backlogged destination keeps reporting even when no alarms arrive."""
        if d["failing"]:
            return True
        if self._state_fn is not None and self._call(self._state_fn, name) is False:
            return True
        if self._spool_fn is not None:
            return ((self._call(self._spool_fn, name) or {}).get("depth", 0)) > 0
        return False

    def metrics_auth_failed(self):
        with self._lock:
            self._metrics_auth_failed += 1

    def metrics(self):
        """Cumulative counters plus current gauges, for GET /metrics. Observer failures read as no data."""
        with self._lock:
            totals = dict(self._totals)
            metrics_auth_failed = self._metrics_auth_failed
            sent = {k: dict(v) for k, v in self._dest_totals.items()}
        dests = {}
        for name in dict.fromkeys([*self._dest_order, *sent]):
            d = {"ok": 0, "failed": 0, "up": None, "depth": None, "oldest_age": None, "evicted": None, "drained": None}
            d.update(sent.get(name, {}))
            if self._state_fn is not None:
                state = self._call(self._state_fn, name)
                d["up"] = None if state is None else state is not False  # failed observer: no sample, not "up"
            if self._spool_fn is not None:
                sp = self._call(self._spool_fn, name)
                if sp is not None:
                    d.update({k: sp.get(k, 0) for k in ("depth", "oldest_age", "evicted", "drained")})
            dests[name] = d
        sp = (self._call(self._spool_fn, None) or {}) if self._spool_fn is not None else {}
        return {
            "counters": totals, "metrics_auth_failed": metrics_auth_failed, "dests": dests, "dedup_store": self._call(lambda _: self._store_mode_fn(), None),
            "spool_enabled": self._spool_fn is not None,
            "spool_bytes": sp.get("bytes", 0), "spool_errors": sp.get("errors", 0),
        }

    def _summary_extra(self):
        if self._spool_fn is None:
            return ""
        sp = self._call(self._spool_fn, None) or {}
        return f" spool_bytes={sp.get('bytes', 0)} spool_errors={sp.get('errors', 0)}"

    def flush(self):
        now = self._clock()
        with self._lock:
            counts = self._counts
            self._counts = dict.fromkeys(_COUNTERS, 0)
            first_reject, self._first_reject = self._first_reject, None
            dests = {k: dict(v) for k, v in self._dest.items()}
            for d in self._dest.values():
                d["ok"] = 0
                d["failed"] = 0
            window = round(now - self._window_start)
            self._window_start = now
        if not any(counts.values()) and not any(self._needs_attention(n, d) for n, d in dests.items()):
            return
        received = counts["received"]
        dup_pct = round(100 * counts["duplicates"] / received) if received else 0
        logger.info(
            "event=summary window=%ds received=%d delivered=%d duplicates=%d dup_pct=%d "
            "undedupable=%d rejected=%d auth_failed=%d send_failed=%d inflight_timeouts=%d dedup_store=%s%s%s",
            window, received, counts["delivered"], counts["duplicates"], dup_pct,
            counts["undedupable"], counts["rejected"], counts["auth_failed"], counts["send_failed"],
            counts["inflight_timeouts"], self._store_mode_fn(), self._summary_extra(),
            f" first_reject={first_reject!r}" if counts["rejected"] and first_reject else "",
        )
        for name, d in dests.items():
            logger.info(
                "event=dest dest=%s ok=%d failed=%d last=%s%s",
                name, d["ok"], d["failed"], d["last"], self._dest_extra(name),
            )
        if counts["auth_failed"]:
            logger.warning(
                "event=auth_failing window=%ds auth_failed=%d",
                window, counts["auth_failed"],
            )
        if received >= STORM_MIN_RECEIVED and dup_pct > STORM_PCT:
            logger.warning(
                "event=duplicate_storm window=%ds received=%d duplicates=%d dup_pct=%d",
                window, received, counts["duplicates"], dup_pct,
            )

    def start(self, interval: float = 60) -> None:
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(interval,), name="obs-summary", daemon=True
        )
        self._thread.start()

    def _run(self, interval):
        while not self._stop.wait(interval):
            try:
                self.flush()
            except Exception as exc:  # the summary must never take the process down
                logger.error("event=summary_error error=%s", type(exc).__name__)

    def stop(self, timeout: float = 5):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def check_destinations(self, destinations):
        for dest in destinations:
            try:
                infos = socket.getaddrinfo(dest.host, dest.port, socket.AF_INET, socket.SOCK_DGRAM)
                logger.info(
                    "event=dest_check dest=%s:%s addr=%s scheme=%s",
                    dest.host, dest.port, infos[0][4][0], dest.scheme,
                )
            except Exception as exc:
                logger.error(
                    "event=dest_check dest=%s:%s error=%s scheme=%s",
                    dest.host, dest.port, _clean(f"{type(exc).__name__}: {exc}"), dest.scheme,
                )


# ---- logging setup -------------------------------------------------------------------------
# stderr is always on. Optional syslog forwarding must never affect request handling: the root
# logger gets a QueueHandler on a bounded queue that drops the oldest line when full, and one
# daemon thread owns the socket. Network errors are swallowed; at most one WARNING per minute.

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
SYSLOG_TAG = "appneta-ia-bridge"
SYSLOG_QUEUE_SIZE = 1000
_SYSLOG_SOCKET_TIMEOUT = 2.0  # seconds; bounds TCP connect and a stalled send
SYSLOG_DRAIN_SECONDS = 3.0  # shutdown budget for all syslog queues together
_SYSLOG_RETRY_SECONDS = 10.0  # after a failure, drop lines instead of reconnecting per line
_SYSLOG_WARN_SECONDS = 60.0

_installed = {"handlers": [], "listeners": []}


class _DropOldestQueueHandler(logging.handlers.QueueHandler):
    """Never blocks and never raises: a full queue loses its oldest line, not the new one."""

    def enqueue(self, record):
        for _ in range(3):
            try:
                self.queue.put_nowait(record)
                return
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    self.queue.get_nowait()  # pyright: ignore[reportAttributeAccessIssue]

    def handleError(self, record):
        pass


class _Backoff(OSError):
    """Raised instead of reconnecting while a recent failure's retry window is open."""


_TCP_FRAME_CHARS = str.maketrans({"\n": " ", "\r": " ", "\x00": " "})  # would split or cut a framed record


class _SyslogForwarder(logging.handlers.SysLogHandler):
    """SysLogHandler with lazy, timeout-bounded connects and swallowed errors.

    The stdlib constructor connects eagerly (and raises on an unresolvable host on Python 3.9),
    so this skips it and opens the socket on first emit, on the forwarding thread.
    """

    def __init__(self, host, port, protocol, warn, clock=time.monotonic):
        logging.Handler.__init__(self)  # not SysLogHandler.__init__: no connect here
        self.address = (host, port)
        self.facility = self.LOG_USER
        self.socktype = socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM
        self.unixsocket = False
        self.socket = None
        self.ident = SYSLOG_TAG + ": "
        self.append_nul = False
        self.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        self._sockaddr = None
        self._warn = warn
        self._clock = clock
        self._retry_at = 0.0
        self._last_warn = None

    def _connect(self):
        if self._clock() < self._retry_at:
            raise _Backoff()
        host, port = self.address
        infos = socket.getaddrinfo(host, port, 0, self.socktype)
        err = None
        for af, socktype, proto, _, sockaddr in infos:
            sock = socket.socket(af, socktype, proto)
            sock.settimeout(_SYSLOG_SOCKET_TIMEOUT)
            try:
                if socktype == socket.SOCK_STREAM:
                    sock.connect(sockaddr)
            except OSError as exc:
                sock.close()
                err = exc
                continue
            self.socket, self._sockaddr = sock, sockaddr
            return sock
        raise err or OSError("getaddrinfo returned no addresses")

    def emit(self, record):
        try:
            msg = self.ident + self.format(record)
            if self.socktype == socket.SOCK_STREAM:
                msg = msg.translate(_TCP_FRAME_CHARS) + "\n"  # newline-framed for TCP collectors
            prio = self.encodePriority(self.facility, self.mapPriority(record.levelname))
            data = f"<{prio}>{msg}".encode()
            sock = self.socket if self.socket is not None else self._connect()
            if self.socktype == socket.SOCK_DGRAM:
                sock.sendto(data, self._sockaddr)  # pyright: ignore[reportArgumentType]
            else:
                sock.sendall(data)
        except Exception:
            self.handleError(record)

    def handleError(self, record):
        exc = sys.exc_info()[1]
        if isinstance(exc, _Backoff):
            return
        self._drop_socket()
        now = self._clock()
        self._retry_at = now + _SYSLOG_RETRY_SECONDS
        if self._last_warn is None or now - self._last_warn >= _SYSLOG_WARN_SECONDS:
            self._last_warn = now
            host, port = self.address
            proto = "tcp" if self.socktype == socket.SOCK_STREAM else "udp"
            reason = _clean(f"{type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                self._warn(
                    f"event=log_forward_failed target={host}:{port} proto={proto} error={reason}"
                )

    def _drop_socket(self):
        sock, self.socket = self.socket, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()

    def close(self):
        with self.lock:  # pyright: ignore[reportOptionalContextManager]
            self._drop_socket()
        logging.Handler.close(self)


class _ForwardListener:
    """QueueListener-style daemon thread; stopping never blocks on a full queue."""

    _STOP = object()

    def __init__(self, q, handler):
        self._queue = q
        self._handler = handler
        self._thread = threading.Thread(target=self._run, name="syslog-forward", daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while True:
            record = self._queue.get()
            if record is self._STOP:  # records queued before it (the final summary) go out first
                return
            with contextlib.suppress(Exception):  # forwarding must never kill this thread
                self._handler.handle(record)

    def request_stop(self):
        _DropOldestQueueHandler(self._queue).enqueue(self._STOP)  # pyright: ignore[reportArgumentType]

    def finish(self, timeout):
        """Wait up to ``timeout`` for the queue to drain, then close; the thread is a daemon."""
        self._thread.join(timeout=timeout)
        self._handler.close()


def shutdown_logging(timeout: float = SYSLOG_DRAIN_SECONDS) -> None:
    """Removes the handlers setup_logging installed and stops the forwarding threads.

    Every target drains its queue in parallel against one ``timeout``, so a dead target cannot
    stretch shutdown past the container's stop grace period.
    """
    root = logging.getLogger()
    for handler in _installed["handlers"]:
        root.removeHandler(handler)
        handler.close()
    _installed["handlers"] = []
    listeners, _installed["listeners"] = _installed["listeners"], []
    for listener in listeners:
        listener.request_stop()
    deadline = time.monotonic() + timeout
    for listener in listeners:
        listener.finish(max(0.0, deadline - time.monotonic()))


def setup_logging(level: str, syslog_targets=(), protocol: str = "udp") -> None:
    """Configures the root logger: stderr always; syslog forwarding to each (host, port) target.

    Each target gets its own bounded queue, forwarder and thread, so a stalled or dead target
    can delay only itself. Idempotent: a second call replaces the handlers (and threads).
    """
    shutdown_logging()
    root = logging.getLogger()
    root.setLevel(level)
    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    root.addHandler(stderr_handler)
    _installed["handlers"].append(stderr_handler)

    def warn(message):
        # Straight to stderr: a forwarding problem must not be queued for the failing forwarder.
        if root.isEnabledFor(logging.WARNING):
            stderr_handler.handle(
                logger.makeRecord(logger.name, logging.WARNING, __file__, 0, message, (), None)
            )

    for host, port in syslog_targets:
        q = queue.Queue(maxsize=SYSLOG_QUEUE_SIZE)
        listener = _ForwardListener(q, _SyslogForwarder(host, port, protocol, warn))
        listener.start()
        queue_handler = _DropOldestQueueHandler(q)
        root.addHandler(queue_handler)
        _installed["handlers"].append(queue_handler)
        _installed["listeners"].append(listener)
