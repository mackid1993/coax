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
# GPU drivers for hardware transcoding:
#   Intel: intel-media-va-driver-non-free (iHD, Broadwell and newer, non-free) and i965-va-driver
#          (older Intel graphics). Both exist only for amd64, so they are installed there only.
#   AMD:   mesa-va-drivers (radeonsi VAAPI), every architecture.
#   NVIDIA: nothing to install; the NVIDIA Container Toolkit injects the driver libraries at run time.
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
    if [ "$arch" = "amd64" ]; then extra="intel-media-va-driver-non-free i965-va-driver"; fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates tzdata \
      mesa-va-drivers \
      tini \
      vainfo \
      $extra; \
    apt-get dist-clean; \
    rm -rf /var/lib/apt/lists/*

# ffmpeg: Jellyfin's build (ffmpeg 8.1), the same generation Dispatcharr ships. Debian's 7.1 lacks the
# full GPU filter set (CUDA deinterlace/scale) coax uses to keep the whole transcode on the GPU.
# Pinned by version and SHA-256 per architecture; bump all three together.
ARG JELLYFIN_FFMPEG=8.1.3-1
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) sha=fbef9f81a53e175194e3f67832618a86111f7bd37197df21a5d748c34289f9c0 ;; \
      arm64) sha=4af743bd776eda082d40c851a4201e51a77a258145dd6ebe164dade39b35b2d0 ;; \
      *) echo "unsupported architecture $arch"; exit 1 ;; \
    esac; \
    deb="jellyfin-ffmpeg8_${JELLYFIN_FFMPEG}-trixie_${arch}.deb"; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
      "https://repo.jellyfin.org/debian/pool/main/j/jellyfin-ffmpeg/$deb" /tmp/ffmpeg.deb; \
    echo "$sha  /tmp/ffmpeg.deb" | sha256sum -c -; \
    apt-get update; \
    apt-get install -y --no-install-recommends /tmp/ffmpeg.deb; \
    rm -f /tmp/ffmpeg.deb; \
    apt-get dist-clean; \
    rm -rf /var/lib/apt/lists/*; \
    ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg; \
    ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe; \
    ffmpeg -hide_banner -version | head -1

# Fail the build if this ffmpeg cannot drive Intel/AMD (VAAPI) or NVIDIA (NVENC) hardware encoders.
# Without this, a changed Debian build would silently leave only CPU encoding.
RUN set -eux; \
    encoders="$(ffmpeg -hide_banner -encoders)"; \
    echo "$encoders" | grep -q ' h264_vaapi '; \
    echo "$encoders" | grep -q ' h264_nvenc '; \
    filters="$(ffmpeg -hide_banner -filters)"; \
    for f in deinterlace_vaapi scale_vaapi bwdif_cuda scale_cuda; do echo "$filters" | grep -q " $f "; done

# NVIDIA Container Toolkit settings (read by the container runtime, not by coax): expose all GPUs and the
# driver libraries needed for NVENC/NVDEC. They do nothing on hosts without an NVIDIA GPU.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

WORKDIR /opt/coax

COPY requirements.txt ./
RUN pip install --require-hashes --no-deps --only-binary=:all: -r requirements.txt

# Non-root user coax (uid/gid 1000). "video" (GID 44 in Debian) is added here; the host's render group
# GID differs between systems, so docker-entrypoint.sh adds the group of /dev/dri/* at start, and any
# compose group_add GIDs are kept as well.
RUN set -eux; \
    groupadd --system --gid 1000 coax; \
    useradd --system --uid 1000 --gid coax --groups video \
      --home-dir /data --no-create-home --shell /usr/sbin/nologin coax; \
    mkdir -p /data; \
    chown coax:coax /data

COPY app ./app
COPY --chmod=0755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# No USER line on purpose. The container starts as root only so docker-entrypoint.sh can chown /data
# (Docker creates a missing ./data owned by root), then it drops to uid/gid 1000 with setpriv before
# starting coax. coax never runs as root. Running with compose "user: 1000:1000" also works; the
# entrypoint then skips the chown and just starts coax.
VOLUME ["/data"]
EXPOSE 7780

# The UI port defaults to 7780. If you change server.port in the UI, the
# health check still probes 7780, so change it here as well (or ignore it).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:7780/healthz', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
