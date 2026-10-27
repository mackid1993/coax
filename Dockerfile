# syntax=docker/dockerfile:1
# coax image. Everything that goes into it is pinned:
#   base image by digest, Debian packages from one snapshot.debian.org date,
#   Python packages by exact version and hash.

# python:3.13-slim-trixie (Python 3.13.18) resolved from Docker Hub on 2026-10-27.
FROM python:3.13-slim-trixie@sha256:b3575dcf816cdacc5292df77a6e3e59b8941631078373a045efa2ac1e9c5c3b2

# Debian snapshot date. Bump deliberately, together with the base image digest.
ARG DEBIAN_SNAPSHOT=20261026T000000Z

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Point apt at the fixed snapshot only. Snapshot Release files are past their
# Valid-Until date by design, so that check is turned off for this archive.
# intel-media-va-driver-non-free lives in non-free and only exists for amd64.
RUN set -eux; \
    rm -f /etc/apt/sources.list /etc/apt/sources.list.d/*; \
    printf '%s\n' \
      'Types: deb' \
      "URIs: http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}" \
      'Suites: trixie trixie-updates' \
      'Components: main non-free non-free-firmware' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
      '' \
      'Types: deb' \
      "URIs: http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}" \
      'Suites: trixie-security' \
      'Components: main non-free non-free-firmware' \
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
      > /etc/apt/sources.list.d/snapshot.sources; \
    printf '%s\n' \
      'Acquire::Check-Valid-Until "false";' \
      'Acquire::Retries "5";' \
      > /etc/apt/apt.conf.d/99snapshot; \
    arch="$(dpkg --print-architecture)"; \
    extra=""; \
    if [ "$arch" = "amd64" ]; then extra="intel-media-va-driver-non-free"; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg \
      mesa-va-drivers \
      tini \
      vainfo \
      $extra; \
    apt-get dist-clean; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/coax

COPY requirements.txt ./
RUN pip install --require-hashes --no-deps --only-binary=:all: -r requirements.txt

# Non-root user. "video" (GID 44 in Debian) is added here; the host's render
# group GID differs between systems, so pass it with group_add in compose.
RUN set -eux; \
    groupadd --system --gid 1000 coax; \
    useradd --system --uid 1000 --gid coax --groups video \
      --home-dir /data --no-create-home --shell /usr/sbin/nologin coax; \
    mkdir -p /data; \
    chown coax:coax /data

COPY app ./app

USER coax
VOLUME ["/data"]
EXPOSE 7780

# The UI port defaults to 7780. If you change server.port in the UI, the
# health check still probes 7780, so change it here as well (or ignore it).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7780/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "app.main"]
