# Base image is pinned by digest (multi-arch manifest list) so builds are
# reproducible. To bump: get the new digest with
#   curl -sI -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
#     https://registry.access.redhat.com/v2/ubi9/ubi-minimal/manifests/latest
# (read docker-content-digest), confirm python3.11-cryptography is still in the
# image (microdnf repoquery), rerun Trivy on the build, and record the new
# digest in CHANGELOG.
# Pinned: ubi9/ubi-minimal, November 2026.
FROM registry.access.redhat.com/ubi9/ubi-minimal@sha256:d8162789d7913ef8941481687b92ab4bbec311870948f66cf52b9cc7e0c57c06

WORKDIR /app
COPY alarm_bridge/ ./alarm_bridge/

# Red Hat UBI 9: freely redistributable, no subscription, and its OpenSSL is
# the same package a RHEL host runs -- the base for FIPS-mode deployments.
# No pip install: alarm_bridge is stdlib-only apart from cryptography, which
# comes from the OS package python3.11-cryptography (RHEL-patched, tied to the
# system OpenSSL). nginx is an
# OS package, not a Python dependency -- it terminates TLS in front of
# alarm_bridge so the whole thing ships as one container. See README.md,
# "TLS termination". shadow-utils (useradd) is not in ubi-minimal;
# entrypoint.sh needs `python` on PATH, UBI only ships python3.11.
#
# The container runs as one fixed, high, unprivileged uid (10001), never root.
# /app stays root-owned and read-only to it; the only place it may write is
# conf.d, where entrypoint.sh drops the selected nginx config, /tmp, and the
# /var/lib/appneta-ia-bridge, a declared VOLUME and the default DEDUP_STATE_DIR,
# where the de-duplication store and the spool of unsent traps persist (mount a
# named volume or host directory there to survive container replacement).
RUN microdnf install -y --nodocs --setopt=install_weak_deps=0 \
        python3.11 python3.11-cryptography nginx shadow-utils \
    && microdnf clean all \
    && ln -s /usr/bin/python3.11 /usr/local/bin/python \
    && useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser \
    && chown appuser:appuser /etc/nginx/conf.d \
    && mkdir -p /var/lib/appneta-ia-bridge \
    && chown appuser:appuser /var/lib/appneta-ia-bridge

COPY docker/nginx.conf /etc/nginx/nginx.conf

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

# nginx listens on :8443 (an unprivileged port); publish it as 443 with
# `-p 443:8443`. alarm_bridge binds loopback only: nginx (same container) is the only way in, so
# the TLS and its auth cannot be bypassed by publishing or reaching port 8080.
# Set LISTEN_HOST=0.0.0.0 to debug it directly (see README.md).
ENV LISTEN_PORT=8080 \
    LISTEN_HOST=127.0.0.1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEDUP_STATE_DIR=/var/lib/appneta-ia-bridge
VOLUME /var/lib/appneta-ia-bridge
EXPOSE 8443

USER 10001

# A TCP connect to alarm_bridge's own port is the strongest check available
# without sending an authenticated request. -I keeps cwd and user site-packages
# off sys.path.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -I -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('LISTEN_PORT', '8080'))), 3).close()" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
