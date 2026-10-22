"""HTTP webhook listener wiring auth, alarm mapping, and trap sending together."""

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from alarm_bridge import mapping
from alarm_bridge.auth import AuthError, validate_auth
from alarm_bridge.snmp_trap import SNMP_TRAP_ADDRESS_OID

logger = logging.getLogger("alarm_bridge")

# Defense in depth: nginx already caps bodies at 1m, but :8080 is also EXPOSEd.
MAX_BODY_BYTES = 1024 * 1024


def make_handler(config, trap_sender):
    class WebhookHandler(BaseHTTPRequestHandler):
        # Per-connection socket timeout: a client that stalls mid-request
        # (slowloris) can't pin a thread forever.
        timeout = 15

        def log_message(self, fmt, *args):
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_POST(self):
            try:
                content_length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                content_length = -1
            if content_length < 0:
                self.send_response(400)
                self.end_headers()
                return
            if content_length > MAX_BODY_BYTES:
                self.send_response(413)
                self.end_headers()
                return

            try:
                validate_auth(
                    self.headers,
                    config.auth_type,
                    username=config.auth_username,
                    password=config.auth_password,
                    token=config.auth_token,
                )
            except AuthError:
                self.send_response(401)
                self.end_headers()
                return

            body = self.rfile.read(content_length) if content_length else b""
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.end_headers()
                return

            trap_by_scheme = {}
            sent = False
            for dest in config.trap_destinations:
                if dest.scheme not in trap_by_scheme:
                    try:
                        if dest.scheme == "general":
                            trap_by_scheme[dest.scheme] = mapping.map_alarm_to_general_trap(payload)
                        else:
                            trap_by_scheme[dest.scheme] = mapping.map_alarm_to_trap(
                                payload,
                                v3_id_offset=config.v3_id_offset,
                            )
                    except mapping.AlarmMappingError as exc:
                        logger.warning("Rejected alarm payload for scheme %s: %s", dest.scheme, exc)
                        trap_by_scheme[dest.scheme] = None

                mapped = trap_by_scheme[dest.scheme]
                if mapped is None:
                    continue
                trap_oid, varbinds = mapped
                # New list per destination: the cached varbinds are shared by
                # every destination on this scheme and must not be mutated.
                if dest.source_address:
                    send_varbinds = varbinds + [
                        (SNMP_TRAP_ADDRESS_OID, dest.source_address, "ipaddress")
                    ]
                else:
                    send_varbinds = varbinds
                try:
                    trap_sender(dest.host, dest.port, dest.community, trap_oid, send_varbinds)
                    sent = True
                except Exception:
                    logger.warning(
                        "Failed to send trap to %s:%s", dest.host, dest.port, exc_info=True
                    )

            if not sent:
                self.send_response(400)
                self.end_headers()
                return

            self.send_response(200)
            self.end_headers()

    return WebhookHandler


def run_server(config, trap_sender) -> None:
    if config.auth_type == "NONE":
        logger.warning("AUTH_TYPE=NONE: webhook accepts unauthenticated requests from anyone who can reach it")
    handler = make_handler(config, trap_sender)
    server = ThreadingHTTPServer(("0.0.0.0", config.listen_port), handler)
    logger.info("appneta-ia-bridge listening on port %s", config.listen_port)
    server.serve_forever()
