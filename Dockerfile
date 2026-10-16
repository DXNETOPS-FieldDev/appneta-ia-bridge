FROM python:3.11-slim

WORKDIR /app
COPY alarm_bridge/ ./alarm_bridge/

# No pip install: alarm_bridge itself is intentionally stdlib-only. nginx is an
# OS package, not a Python dependency -- it terminates TLS in front of
# alarm_bridge so the whole thing ships as one container. See README.md,
# "TLS termination".
RUN useradd --no-create-home --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app \
    && apt-get update \
    && apt-get install -y --no-install-recommends nginx \
    && rm -rf /var/lib/apt/lists/* /etc/nginx/sites-enabled/default

# Both configs are staged, unused, at build time -- OUTSIDE conf.d, so nginx
# never loads the losing one -- and entrypoint.sh copies one (based on
# TLS_ENABLED) to conf.d/appneta-ia-bridge.conf on every container start. The
# templates must survive a start: `docker restart` (or a dockerd restart
# during a package upgrade) re-runs the entrypoint on the SAME filesystem.
# See entrypoint.sh and README.md, "TLS termination".
COPY docker/nginx-tls.conf /etc/nginx/appneta-ia-bridge-templates/tls.conf
COPY docker/nginx-plain.conf /etc/nginx/appneta-ia-bridge-templates/plain.conf
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# alarm_bridge itself always runs as appuser (see entrypoint.sh) -- the
# container's top-level process starts as root only because nginx's master
# needs it to bind the privileged :443 port; nginx drops its own worker
# processes to an unprivileged user internally, the same way it always does.
ENV LISTEN_PORT=8080
EXPOSE 8080 443

ENTRYPOINT ["/entrypoint.sh"]
