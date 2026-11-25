import tempfile
import threading
import unittest
from typing import ClassVar
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from alarm_bridge import metrics
from alarm_bridge.config import Config, ConfigError, TrapDestination, load_config
from alarm_bridge.obs import Stats
from alarm_bridge.server import make_handler

TOKEN = "t0ken"
RO_TOKEN = "r0-t0ken"
DEST = "192.0.2.5:162"


def _stats(**kwargs):
    return Stats([DEST], lambda: "sqlite", **kwargs)


class TestStatsMetrics(unittest.TestCase):
    def test_totals_are_cumulative_across_summary_windows(self):
        stats = _stats()
        stats.received()
        stats.received()
        stats.send_ok(DEST)
        stats.send_failed(DEST, "timeout")
        stats.flush()  # the 60 s summary zeroes its window counters
        stats.received()
        snap = stats.metrics()
        self.assertEqual(snap["counters"]["received"], 3)
        self.assertEqual(snap["counters"]["send_failed"], 1)
        self.assertEqual(snap["dests"][DEST]["ok"], 1)
        self.assertEqual(snap["dests"][DEST]["failed"], 1)

    def test_destination_gauges_come_from_the_observers(self):
        spool = {"depth": 3, "oldest_age": 12.5, "evicted": 2, "drained": 7, "bytes": 4096, "errors": 1}
        stats = _stats(spool_fn=lambda name: spool, state_fn=lambda name: False)
        snap = stats.metrics()
        self.assertFalse(snap["dests"][DEST]["up"])
        self.assertEqual(snap["dests"][DEST]["depth"], 3)
        self.assertEqual(snap["dests"][DEST]["drained"], 7)
        self.assertEqual(snap["spool_bytes"], 4096)
        self.assertEqual(snap["spool_errors"], 1)
        self.assertEqual(snap["dedup_store"], "sqlite")

    def test_a_failing_observer_reads_as_no_data_not_an_exception(self):
        def boom(name):
            raise RuntimeError("spool closed")

        snap = Stats([DEST], boom, spool_fn=boom, state_fn=boom).metrics()
        self.assertIsNone(snap["dests"][DEST]["depth"])
        self.assertIsNone(snap["dests"][DEST]["up"])
        self.assertIsNone(snap["dedup_store"])
        text = metrics.render(snap)  # absent, never a healthy-looking 0 or 1
        self.assertNotIn("destination_up{", text)
        self.assertNotIn("spool_depth{", text)
        self.assertNotIn("dedup_store_mode{", text)
        self.assertIn("webhooks_received_total 0", text)


class TestRender(unittest.TestCase):
    def _snap(self, **over):
        snap = {
            "counters": {"received": 5, "delivered": 4, "duplicates": 1, "undedupable": 0, "rejected": 0,
                         "auth_failed": 2, "send_failed": 3, "inflight_timeouts": 0},
            "metrics_auth_failed": 4,
            "dests": {DEST: {"ok": 9, "failed": 3, "up": True, "depth": 2, "oldest_age": 1.5,
                             "evicted": 0, "drained": 4}},
            "spool_enabled": True, "spool_bytes": 100, "spool_errors": 0, "dedup_store": "sqlite",
        }
        snap.update(over)
        return snap

    def test_prometheus_text_format(self):
        text = metrics.render(self._snap())
        self.assertTrue(text.endswith("\n"))
        self.assertIn("# TYPE appneta_ia_bridge_webhooks_received_total counter", text)
        self.assertIn("appneta_ia_bridge_webhooks_received_total 5\n", text)
        self.assertIn('appneta_ia_bridge_trap_sends_total{destination="192.0.2.5:162",result="failed"} 3\n', text)
        self.assertIn('appneta_ia_bridge_destination_up{destination="192.0.2.5:162"} 1\n', text)
        self.assertIn('appneta_ia_bridge_spool_depth{destination="192.0.2.5:162"} 2\n', text)
        self.assertIn('appneta_ia_bridge_dedup_store_mode{mode="sqlite"} 1\n', text)
        self.assertIn("appneta_ia_bridge_metrics_auth_failures_total 4\n", text)

    def test_every_sample_has_help_and_type(self):
        text = metrics.render(self._snap())
        names = {line.split("{")[0].split(" ")[0] for line in text.splitlines() if line and line[0] != "#"}
        for name in names:
            self.assertIn(f"# HELP {name} ", text)
            self.assertIn(f"# TYPE {name} ", text)

    def test_label_values_are_escaped(self):
        text = metrics.render(self._snap(dests={'a"b\\c\nd': {"ok": 1, "failed": 0, "up": True, "depth": 0,
                                                              "oldest_age": 0, "evicted": 0, "drained": 0}}))
        self.assertIn(r'destination="a\"b\\c\nd"', text)
        self.assertNotIn("c\nd", text)

    def test_spool_series_absent_when_spooling_is_off(self):
        text = metrics.render(self._snap(spool_enabled=False))
        self.assertNotIn("spool_depth", text)
        self.assertNotIn("destination_up", text)
        self.assertIn("webhooks_received_total", text)


class TestMetricsEndpoint(unittest.TestCase):
    def _start(self, with_stats=True, metrics_token=RO_TOKEN, auth_type="BEARER"):
        config = Config(
            listen_port=0, auth_type=auth_type, auth_username="u", auth_password="p", auth_token=TOKEN,
            trap_destinations=[TrapDestination(host="127.0.0.1", port=162, community="public", scheme="vna")],
            v3_id_offset=None, metrics_auth_token=metrics_token,
        )
        self.stats = _stats()
        handler = make_handler(config, lambda *a: None, stats=self.stats if with_stats else None)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.httpd.shutdown)

    def _get(self, path="/metrics", auth=True, token=RO_TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if auth else {}
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=2)
            return response.status, response.headers, response.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read().decode()

    def test_authenticated_scrape_returns_prometheus_text(self):
        self._start()
        self.stats.received()
        status, headers, body = self._get()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; version=0.0.4; charset=utf-8")
        self.assertIn("appneta_ia_bridge_webhooks_received_total 1\n", body)

    def test_scrape_without_credentials_is_401_and_not_a_webhook_auth_failure(self):
        self._start()
        status, _, body = self._get(auth=False)
        self.assertEqual(status, 401)
        self.assertNotIn("appneta_ia_bridge", body)
        snap = self.stats.metrics()
        self.assertEqual(snap["counters"]["auth_failed"], 0)
        self.assertEqual(snap["metrics_auth_failed"], 1)

    def test_the_webhook_token_does_not_open_metrics(self):
        self._start()
        self.assertEqual(self._get(token=TOKEN)[0], 401)
        self.assertEqual(self.stats.metrics()["metrics_auth_failed"], 1)

    def test_read_only_token_is_bearer_whatever_the_webhook_auth_type(self):
        self._start(auth_type="BASIC")
        self.assertEqual(self._get()[0], 200)

    def test_read_only_token_does_not_authenticate_a_webhook_post(self):
        self._start()
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/", data=b"{}", headers={"Authorization": f"Bearer {RO_TOKEN}"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request, timeout=2)
        self.assertEqual(ctx.exception.code, 401)

    def test_no_metrics_token_means_no_endpoint_even_with_the_webhook_credentials(self):
        self._start(metrics_token="")
        self.assertEqual(self._get(token=TOKEN)[0], 404)
        self.assertEqual(self._get(auth=False)[0], 404)

    def test_other_get_paths_are_404(self):
        self._start()
        self.assertEqual(self._get("/")[0], 404)
        self.assertEqual(self._get("/metrics/extra")[0], 404)

    def test_query_string_is_ignored(self):
        self._start()
        self.assertEqual(self._get("/metrics?x=1")[0], 200)

    def test_without_stats_the_endpoint_is_404(self):
        self._start(with_stats=False)
        self.assertEqual(self._get()[0], 404)


class TestMetricsConfig(unittest.TestCase):
    ENV: ClassVar[dict[str, str]] = {
        "AUTH_TYPE": "BEARER", "AUTH_TOKEN": TOKEN,
        "TRAP_DESTINATION_1_HOST": "192.0.2.5", "TRAP_DESTINATION_1_COMMUNITY": "c",
        "TRAP_DESTINATION_1_SCHEME": "general", "TRAP_DESTINATION_1_SOURCE_ADDRESS": "none",
    }

    def test_no_token_means_metrics_off(self):
        self.assertEqual(load_config(dict(self.ENV)).metrics_auth_token, "")

    def test_read_only_token_from_env_and_file(self):
        self.assertEqual(load_config({**self.ENV, "METRICS_AUTH_TOKEN": RO_TOKEN}).metrics_auth_token, RO_TOKEN)
        with tempfile.NamedTemporaryFile("w", suffix=".tok") as f:
            f.write(RO_TOKEN + "\n")
            f.flush()
            env = {**self.ENV, "METRICS_AUTH_TOKEN_FILE": f.name}
            self.assertEqual(load_config(env).metrics_auth_token, RO_TOKEN)

    def test_read_only_token_equal_to_the_webhook_token_is_refused(self):
        with self.assertRaises(ConfigError) as ctx:
            load_config({**self.ENV, "METRICS_AUTH_TOKEN": TOKEN})
        message = str(ctx.exception)
        self.assertIn("METRICS_AUTH_TOKEN is the same value as AUTH_TOKEN", message)
        self.assertIn("must not be reused", message)
        self.assertNotIn("Missing required", message)
        self.assertNotIn(TOKEN, message)  # names the variables, never the secret


if __name__ == "__main__":
    unittest.main()
