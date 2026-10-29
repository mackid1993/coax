<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/coax-wordmark-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/brand/coax-wordmark-light.svg">
    <img alt="coax" src="assets/brand/coax-wordmark-light.svg" height="72">
  </picture>
</h1>

> **Not affiliated with Fancy Bits, LLC.** coax is an independent, unofficial project. It is not affiliated with, endorsed by, sponsored by, or supported by Fancy Bits, LLC or Channels DVR. Fancy Bits and Channels DVR will not provide support for coax; please do not contact them about it. Channels DVR is a trademark of Fancy Bits, LLC. All other trademarks and product names are the property of their respective owners and are used here only to describe compatibility.

coax is designed specifically for [Channels DVR](https://getchannels.com/). It is a small,
self-hosted IPTV appliance: you give it your IPTV playlists (Xtream Codes logins or M3U URLs), and it gives
your DVR a steady tuner stream for each channel, an M3U with Gracenote station IDs, and an optional XMLTV
guide.

It does one job and is built once: no auto-update, no code downloaded at runtime, no telemetry, no database.
Everything you set lives in one folder, `./data`.

> **LAN only.** coax has no login and no authentication. Your provider passwords are in its config. Never
> expose port 7780 to the internet (no port forwarding, no public reverse proxy). Use it on your home network,
> or reach it remotely through a VPN such as Tailscale or WireGuard.

> [!IMPORTANT]
> **coax is built for HLS. Use a source that offers HLS.** Most of what keeps playback smooth depends on it:
> refetching missed video, a full one-minute reserve, and [Smart Standby](#smart-standby) checking every backup
> before switching so a bad one it detects is never shown. coax supports MPEG-TS and every effort has been
> made to make it work well, but on TS these recovery features are heavily reduced: the reserve is about 20 seconds, video lost
> in an outage is gone, and a bad backup can only be judged after viewers have seen it. If your source only
> offers MPEG-TS, you are better off finding one that offers HLS than relying on coax to make up for it. See
> [HLS or MPEG-TS](#hls-or-mpeg-ts) for the full comparison.


## Screenshots

Channel names, providers and logos below are made up for the demo.

| | |
|---|---|
| ![Channels](assets/screenshots/channels.png) | ![Status](assets/screenshots/status.png) |
| **Channels**: pick streams, match the guide and attach backups in one pass | **Status**: live channels, transcoding, real-time and reserve, and what coax corrected |
| ![Lineups](assets/screenshots/lineups.png) | ![Providers](assets/screenshots/providers.png) |
| **Lineups**: one Channels DVR source each, added with one click | **Providers**: Xtream or M3U, several lines, group picker |
| ![Logs](assets/screenshots/logs.png) | ![Settings](assets/screenshots/settings.png) |
| **Logs**: live, filterable | **Settings**: addresses, transcoding, Auto-match region, backups |

## Contents

- [Features](#features)
- [Run it without a VPN](#run-it-without-a-vpn)
- [Run it with a VPN (Gluetun)](#run-it-with-a-vpn-gluetun)
- [First run](#first-run)
- [Settings reference](#settings-reference)
- [How playback stays smooth](#how-playback-stays-smooth)
- [HLS or MPEG-TS](#hls-or-mpeg-ts)
- [Recordings](#recordings)
- [GPU transcoding](#gpu-transcoding)
- [Updating](#updating)
- [Data folder](#data-folder)
- [Troubleshooting](#troubleshooting)
- [Building from source](#building-from-source)

## Features

- **Xtream Codes and M3U providers**, several side by side, with test connection and scheduled refresh.
- **Group selection** like Dispatcharr: pick which provider groups to bring in from a searchable checklist.
- **Connection pools**: several lines (logins) per provider, each with its own max streams. A tune takes the
  first stream of the channel that has a free line, across providers.
- **Backup streams per channel**, mixed across providers, with automatic failover in the stream engine and
  suggested matches (same name, call sign, or provider guide ID).
- **Tuner sharing**: a second viewer of the same channel shares the running stream and uses no extra line.
- **Smart Standby**: switches to a backup only on real trouble, tries the least troubled backup first, blocks
  streams that keep failing for a while (never deletes them), and moves the stream that proves stable ahead of
  the troubled one (to the top when the troubled one was the main stream). Changes appear under the bell with
  Undo. See [Smart Standby](#smart-standby).
- **Never gives up on a stream.** While Channels DVR is connected, coax keeps recovering: a frozen engine is
  restarted, a stream with no video is retuned (alternating to another free line of the provider when there is one),
  and null packets hold the connection open in between, so a recording keeps going. Streams that failed recently
  are tried last, and each channel's 10-minute health lines are kept in `data/cache/history/`.
- **One-click Add to Channels DVR**: each lineup becomes a Channels DVR source with your choice of guide data,
  stream limit, numbering and logos. **Automatic sync** pushes every later change to the DVR within seconds and
  downloads the guide when it changes, so you never press *Refresh* in Channels DVR.
- **Gracenote guide data** through Channels DVR's own station search, with one-click suggestions, or
  **XMLTV** from your provider.
- **Transcoding (recommended)** to one fixed format (1080p or 720p, 60/30/50/25 fps) on Intel or AMD graphics
  (VAAPI), NVIDIA (NVENC), or the CPU.
- **Optional VPN** for all provider traffic using Gluetun.
- A web UI with a live Status page: sessions, viewers, line usage, throughput, and the log. Each playing
  channel lists all its streams, with **Switch** to move to another one now, **Disable** to stop using one, and
  a grip to drag them into a new order. The UI works on a phone too.

**User agent.** coax always identifies itself to providers as `TiviMate/5.1.6 (Android 12)`. This is
hard-coded and the same for every provider; it is not a setting.

## Run it without a VPN

You need Docker with Docker Compose on a machine on the same network as your Channels DVR server.

Make a folder named `coax`, save `docker-compose.yml` and `.env` in it, and run `docker compose up -d` there.
The compose file needs no editing; every setting is a variable with a default, so it runs with an empty
`.env`. Portainer users: paste the compose file into a stack and put the variables in its Environment
variables section.

<details>
<summary><code>docker-compose.yml</code></summary>

```yaml
services:
  # GitHub home for this project with setup instructions: https://github.com/mackid1993/coax
  coax: # This compose file needs no editing. Put your values in .env (or the Environment variables section of Portainer).
    image: ghcr.io/mackid1993/coax:${TAG:-latest} # latest is the newest release. Set TAG to a version number (for example 1.0.3) to freeze one
    container_name: ${CONTAINER_NAME:-coax}
    runtime: ${DOCKER_RUNTIME:-runc} # GPU transcoding on NVIDIA only. Set DOCKER_RUNTIME=nvidia. Requires the NVIDIA driver and NVIDIA Container Toolkit on the host
    devices:
      - ${GPU_DEVICE:-/dev/null} # GPU transcoding on Intel or AMD only. Set GPU_DEVICE=/dev/dri. The default /dev/null always exists and does nothing
    ports:
      - ${HOST_PORT:-7780}:7780 # Port for the web UI and the streams. Base URL in coax Settings must use this port
    environment:
      - TZ=${TZ:-Etc/UTC} # Your timezone, for log timestamps and the daily backup, for example America/New_York
      - NVIDIA_VISIBLE_DEVICES=${NVIDIA_VISIBLE_DEVICES:-all} # NVIDIA only. all, or one GPU's UUID. Does nothing unless DOCKER_RUNTIME=nvidia
      - NVIDIA_DRIVER_CAPABILITIES=${NVIDIA_DRIVER_CAPABILITIES:-compute,video,utility} # NVIDIA only. Leave at the default
    volumes:
      - ${HOST_DIR:-.}/data:/data # Everything coax keeps (settings, backups, caches) goes in a data folder inside HOST_DIR. Created on first start; coax fixes its ownership itself
    restart: unless-stopped
```

</details>

<details>
<summary><code>.env</code></summary>

```sh
# =====================================================================================================
# coax settings for docker-compose.yml (coax without a VPN)
# =====================================================================================================
# How to use this file:
#   1. Save it in the same folder as docker-compose.yml and name it exactly .env (with the dot).
#   2. Change the values you need. Every value has a default, so a line you leave empty still works.
#   3. Start coax from that folder with:  docker compose up -d
#   4. Open http://<this-machine>:7780 (or the HOST_PORT you chose) in a browser.
#
# This file only controls the container. Your providers, channels, lineups, guide and transcoding
# are all set in the coax web UI and saved in the data folder, not here.
# After changing this file, run  docker compose up -d  again to apply it.
# =====================================================================================================


# ----- TAG -------------------------------------------------------------------------------------------
# Which coax version to run.
#   latest  = always the newest release; "docker compose pull" picks up updates.
#   1.0.3   = a version number freezes coax on that release until you change it.
# Default: latest
TAG=latest


# ----- CONTAINER_NAME --------------------------------------------------------------------------------
# The name Docker gives the coax container (what you see in "docker ps" and in Unraid/Portainer).
# Only change it if you already have a container called coax.
# Default: coax
CONTAINER_NAME=coax


# ----- HOST_PORT -------------------------------------------------------------------------------------
# The port on this machine for the coax web UI and the streams Channels DVR plays.
# The Base URL you enter in coax Settings must use this same port, for example http://192.168.1.10:7780
# Only change it if something else on this machine already uses 7780.
# Default: 7780
HOST_PORT=7780


# ----- HOST_DIR --------------------------------------------------------------------------------------
# The folder on this machine that belongs to coax. Type only the folder, without /data at the end:
# coax adds /data itself and keeps everything there (settings, backups, caches). Examples:
#   Linux:     HOST_DIR=/opt/coax                  ->  files in /opt/coax/data
#   Unraid:    HOST_DIR=/mnt/user/appdata/coax     ->  files in /mnt/user/appdata/coax/data
#   Synology:  HOST_DIR=/volume1/docker/coax       ->  files in /volume1/docker/coax/data
# Leave it empty to keep coax's files in a data folder next to this file.
# The folder is created on first start, and coax fixes its ownership by itself.
# Back this folder up; it is everything coax knows.
# Default: the folder this file is in (so ./data)
HOST_DIR=


# ----- TZ --------------------------------------------------------------------------------------------
# Your timezone, used for log timestamps and for when the daily backup runs.
# Use a name from this list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
# Examples: America/New_York, America/Chicago, America/Denver, America/Los_Angeles, Europe/London
# Default: Etc/UTC
TZ=


# ----- GPU_DEVICE (Intel or AMD graphics) ------------------------------------------------------------
# Lets coax use an Intel or AMD GPU for transcoding (recommended whenever you have one).
#   /dev/dri   = use the Intel or AMD GPU. Then turn on Transcode in coax Settings.
#   empty      = no Intel/AMD GPU. Leave it empty if you have no GPU or an NVIDIA GPU.
# Empty means /dev/null, a harmless placeholder (Docker needs a device that exists).
# Default: /dev/null
GPU_DEVICE=


# ----- DOCKER_RUNTIME (NVIDIA graphics) --------------------------------------------------------------
# Lets coax use an NVIDIA GPU for transcoding.
#   nvidia  = use the NVIDIA GPU. Requires the NVIDIA driver and the NVIDIA Container Toolkit on this
#             machine (on Unraid: the Nvidia Driver plugin). Then turn on Transcode in coax Settings.
#   empty   = Docker's normal runtime (runc). Leave it empty for Intel, AMD or no GPU.
# Do not set both: Intel/AMD uses GPU_DEVICE above, NVIDIA uses this one.
# Default: runc
DOCKER_RUNTIME=
```

</details>

| Variable | Default | What it does |
| --- | --- | --- |
| `TAG` | `latest` | Image version. `latest` is the newest release; set a version number (for example `1.0.3`) to freeze one. |
| `CONTAINER_NAME` | `coax` | Container name. |
| `HOST_PORT` | `7780` | Port for the web UI and the streams. Base URL in coax Settings must use it. |
| `HOST_DIR` | the compose folder | The folder for coax on the host, without `/data` (for example `/opt/coax`, `/mnt/user/appdata/coax` on Unraid, `/volume1/docker/coax` on Synology). coax keeps everything in a `data` folder inside it. |
| `TZ` | `Etc/UTC` | Your timezone, for log timestamps and the daily backup. |
| `GPU_DEVICE`, `DOCKER_RUNTIME` | `/dev/null`, `runc` | GPU transcoding. See [GPU transcoding](#gpu-transcoding). |

Open `http://<this-host>:7780` and continue with [First run](#first-run).

Docker creates the data folder on first start. If it ends up owned by root, coax fixes that itself: the
container starts as root only long enough to `chown` `/data` to uid 1000, then runs coax as uid 1000.

Images are only built and published when the maintainer runs the release workflow by hand.

## Run it with a VPN (Gluetun)

`docker-compose.vpn.yml` runs [Gluetun](https://github.com/qdm12/gluetun) (MIT license, by qdm12) on its
`latest` tag and puts coax in Gluetun's network, so all of coax's traffic (provider logins, playlists,
guides, streams) goes through your VPN. If the tunnel drops, Gluetun blocks traffic rather than leaking it,
and streams stop until it reconnects.

Make a folder named `coax`, save `docker-compose.vpn.yml` and `.env` in it, fill in `.env`, keep it private
(`chmod 600 .env`), and run `docker compose -f docker-compose.vpn.yml up -d` there. The compose file needs no
editing.

<details>
<summary><code>docker-compose.vpn.yml</code></summary>

```yaml
services:
  # GitHub home for this project with setup instructions: https://github.com/mackid1993/coax
  # Gluetun (https://github.com/qdm12/gluetun, MIT) carries all of coax's traffic through your VPN. Its setup guide: https://github.com/qdm12/gluetun-wiki
  # This compose file needs no editing. Put your values in .env (or the Environment variables section of Portainer).
  gluetun:
    image: qmcgaw/gluetun:latest # Not pinned on purpose: Gluetun is your VPN and firewall, so take its security fixes
    container_name: ${VPN_CONTAINER_NAME:-coax-gluetun}
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun:/dev/net/tun
    ports:
      - ${HOST_PORT:-7780}:7780 # coax shares Gluetun's network, so coax's port is published here. Base URL in coax Settings must use this port
    environment:
      # ── Your LAN ────────────────────────────────────────────────────────────
      - FIREWALL_OUTBOUND_SUBNETS=${LAN_SUBNETS:?set LAN_SUBNETS in .env} # Your LAN, for example 192.168.1.0/24, so coax can reach Channels DVR. Required
      # ── VPN ─────────────────────────────────────────────────────────────────
      # Set VPN_SERVICE_PROVIDER and VPN_TYPE, then the values your provider's page lists:
      # https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers. Leave the rest empty; Gluetun ignores empty values.
      - VPN_SERVICE_PROVIDER=${VPN_SERVICE_PROVIDER:?set VPN_SERVICE_PROVIDER in .env} # Your VPN, as named on its Gluetun page: mullvad, protonvpn, nordvpn, surfshark, private internet access, windscribe, ... Required
      - VPN_TYPE=${VPN_TYPE} # openvpn or wireguard. Empty means openvpn. Not every provider supports wireguard
      # OpenVPN
      - OPENVPN_USER=${OPENVPN_USER} # Your provider's OpenVPN or service username (often not your account login; see its Gluetun page)
      - OPENVPN_PASSWORD=${OPENVPN_PASSWORD} # Your provider's OpenVPN or service password
      - OPENVPN_PROTOCOL=${OPENVPN_PROTOCOL} # udp or tcp. Empty means udp
      # WireGuard
      - WIREGUARD_PRIVATE_KEY=${WIREGUARD_PRIVATE_KEY} # [Interface] PrivateKey from your provider's WireGuard config
      - WIREGUARD_ADDRESSES=${WIREGUARD_ADDRESSES} # [Interface] Address, for example 10.64.12.34/32. Needed by most providers
      - WIREGUARD_PRESHARED_KEY=${WIREGUARD_PRESHARED_KEY} # [Peer] PresharedKey. Only if your provider uses one
      - WIREGUARD_PUBLIC_KEY=${WIREGUARD_PUBLIC_KEY} # [Peer] PublicKey. Only for VPN_SERVICE_PROVIDER=custom (a VPN Gluetun has no page for)
      - WIREGUARD_ENDPOINT_IP=${WIREGUARD_ENDPOINT_IP} # [Peer] Endpoint IP. Only for custom. Must be an IP address, not a hostname
      - WIREGUARD_ENDPOINT_PORT=${WIREGUARD_ENDPOINT_PORT} # [Peer] Endpoint port. Only for custom, or if your provider's page asks for it
      # DNS through the VPN (OpenVPN and WireGuard). Set both to use your VPN provider's DNS server, so lookups stay in the tunnel
      - DNS_UPSTREAM_RESOLVER_TYPE=${DNS_UPSTREAM_RESOLVER_TYPE} # plain to use DNS_UPSTREAM_PLAIN_ADDRESSES. Empty means Gluetun's encrypted DNS (DNS over TLS to Cloudflare)
      - DNS_UPSTREAM_PLAIN_ADDRESSES=${DNS_UPSTREAM_PLAIN_ADDRESSES} # Your VPN provider's DNS server WITH the port, IP:53, for example 10.255.255.1:53. Gluetun refuses a bare IP. Never a LAN address: that leaks DNS outside the tunnel
      # Gluetun's DNS content filters, off on purpose: their block lists can block IPTV provider, guide and logo domains, which breaks playback
      - BLOCK_MALICIOUS=${BLOCK_MALICIOUS:-off} # Malware and phishing block list. Gluetun's own default is on; coax turns it off because it can block IPTV hosts
      - BLOCK_SURVEILLANCE=${BLOCK_SURVEILLANCE:-off} # Tracking and telemetry block list
      - BLOCK_ADS=${BLOCK_ADS:-off} # Ad block list. Can block ad-inserted (SSAI) streams and some guide and logo servers
      # Server choice (optional). Comma separated; the names are on your provider's Gluetun page
      - SERVER_COUNTRIES=${SERVER_COUNTRIES} # For example Netherlands
      - SERVER_REGIONS=${SERVER_REGIONS} # For example Europe
      - SERVER_CITIES=${SERVER_CITIES} # For example Amsterdam
      - SERVER_HOSTNAMES=${SERVER_HOSTNAMES} # One exact server, for example nl-ams-wg-001
      - TZ=${TZ:-Etc/UTC} # Your timezone, for Gluetun's log timestamps
    restart: unless-stopped

  coax:
    image: ghcr.io/mackid1993/coax:${TAG:-latest} # latest is the newest release. Set TAG to a version number (for example 1.0.3) to freeze one
    container_name: ${CONTAINER_NAME:-coax}
    network_mode: service:gluetun # All of coax's traffic goes through Gluetun. No ports here; they are on the gluetun service
    depends_on:
      - gluetun
    runtime: ${DOCKER_RUNTIME:-runc} # GPU transcoding on NVIDIA only. Set DOCKER_RUNTIME=nvidia. Requires the NVIDIA driver and NVIDIA Container Toolkit on the host
    devices:
      - ${GPU_DEVICE:-/dev/null} # GPU transcoding on Intel or AMD only. Set GPU_DEVICE=/dev/dri. The default /dev/null always exists and does nothing
    environment:
      - TZ=${TZ:-Etc/UTC} # Your timezone, for log timestamps and the daily backup, for example America/New_York
      - NVIDIA_VISIBLE_DEVICES=${NVIDIA_VISIBLE_DEVICES:-all} # NVIDIA only. all, or one GPU's UUID. Does nothing unless DOCKER_RUNTIME=nvidia
      - NVIDIA_DRIVER_CAPABILITIES=${NVIDIA_DRIVER_CAPABILITIES:-compute,video,utility} # NVIDIA only. Leave at the default
    volumes:
      - ${HOST_DIR:-.}/data:/data # Everything coax keeps (settings, backups, caches) goes in a data folder inside HOST_DIR. Created on first start; coax fixes its ownership itself
    restart: unless-stopped
```

</details>

<details>
<summary><code>.env</code></summary>

```sh
# =====================================================================================================
# coax + Gluetun settings for docker-compose.vpn.yml (coax behind a VPN)
# =====================================================================================================
# How to use this file:
#   1. Save it in the same folder as docker-compose.vpn.yml and name it exactly .env (with the dot).
#   2. Fill in the REQUIRED values (LAN_SUBNETS, VPN_SERVICE_PROVIDER) and the values your VPN needs.
#   3. Keep it private, it holds your VPN keys and passwords:  chmod 600 .env
#   4. Start from that folder with:  docker compose -f docker-compose.vpn.yml up -d
#   5. Open http://<this-machine>:7780 (or the HOST_PORT you chose) in a browser.
#
# Gluetun (https://github.com/qdm12/gluetun) is the VPN container. All of coax's traffic goes through it,
# and if the VPN drops, Gluetun blocks traffic instead of leaking it.
# VPN values left empty are ignored by Gluetun, so only fill in what your VPN provider needs.
# If the stack will not start, run  docker logs coax-gluetun  ; Gluetun says exactly which value is wrong.
#
# This file only controls the containers. Providers, channels, lineups, guide and transcoding are set
# in the coax web UI. After changing this file, run the up -d command again to apply it.
# =====================================================================================================


# #####################################################################################################
# PART 1: coax
# #####################################################################################################

# ----- TAG -------------------------------------------------------------------------------------------
# Which coax version to run.
#   latest  = always the newest release; "docker compose pull" picks up updates.
#   1.0.3   = a version number freezes coax on that release until you change it.
# (Gluetun always runs its latest version on purpose, so it keeps getting security fixes.)
# Default: latest
TAG=latest


# ----- CONTAINER_NAME and VPN_CONTAINER_NAME ---------------------------------------------------------
# The names Docker gives the two containers (what you see in "docker ps" and in Unraid/Portainer).
# Only change them if you already have containers with these names.
# Defaults: coax and coax-gluetun
CONTAINER_NAME=coax
VPN_CONTAINER_NAME=coax-gluetun


# ----- HOST_PORT -------------------------------------------------------------------------------------
# The port on this machine for the coax web UI and the streams Channels DVR plays. Because coax lives
# inside Gluetun's network, this port is published on the Gluetun container; that is normal.
# The Base URL you enter in coax Settings must use this same port, for example http://192.168.1.10:7780
# Default: 7780
HOST_PORT=7780


# ----- HOST_DIR --------------------------------------------------------------------------------------
# The folder on this machine that belongs to coax. Type only the folder, without /data at the end:
# coax adds /data itself and keeps everything there (settings, backups, caches). Examples:
#   Linux:     HOST_DIR=/opt/coax                  ->  files in /opt/coax/data
#   Unraid:    HOST_DIR=/mnt/user/appdata/coax     ->  files in /mnt/user/appdata/coax/data
#   Synology:  HOST_DIR=/volume1/docker/coax       ->  files in /volume1/docker/coax/data
# Leave it empty to keep coax's files in a data folder next to this file.
# The folder is created on first start, and coax fixes its ownership by itself. Back it up.
# Default: the folder this file is in (so ./data)
HOST_DIR=


# ----- TZ --------------------------------------------------------------------------------------------
# Your timezone, used by both containers for log timestamps and by coax for the daily backup.
# Use a name from this list: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones
# Examples: America/New_York, America/Chicago, America/Denver, America/Los_Angeles, Europe/London
# Default: Etc/UTC
TZ=


# ----- GPU_DEVICE (Intel or AMD graphics) ------------------------------------------------------------
# Lets coax use an Intel or AMD GPU for transcoding (recommended whenever you have one).
#   /dev/dri   = use the Intel or AMD GPU. Then turn on Transcode in coax Settings.
#   empty      = no Intel/AMD GPU. Leave it empty if you have no GPU or an NVIDIA GPU.
# Empty means /dev/null, a harmless placeholder (Docker needs a device that exists).
# Default: /dev/null
GPU_DEVICE=


# ----- DOCKER_RUNTIME (NVIDIA graphics) --------------------------------------------------------------
# Lets coax use an NVIDIA GPU for transcoding.
#   nvidia  = use the NVIDIA GPU. Requires the NVIDIA driver and the NVIDIA Container Toolkit on this
#             machine (on Unraid: the Nvidia Driver plugin). Then turn on Transcode in coax Settings.
#   empty   = Docker's normal runtime (runc). Leave it empty for Intel, AMD or no GPU.
# Do not set both: Intel/AMD uses GPU_DEVICE above, NVIDIA uses this one.
# Default: runc
DOCKER_RUNTIME=


# #####################################################################################################
# PART 2: your network (REQUIRED)
# #####################################################################################################

# ----- LAN_SUBNETS (REQUIRED) ------------------------------------------------------------------------
# Your home network, so coax can reach Channels DVR and your browser can reach coax. Without it,
# Gluetun's firewall blocks your LAN and nothing works. The stack refuses to start if this is empty.
# Write it as a network range, not a single address. If your devices are 192.168.1.x, use 192.168.1.0/24.
# Several networks: separate with commas, for example 192.168.1.0/24,10.0.10.0/24
# It must not overlap the VPN's own addresses (for example WIREGUARD_ADDRESSES below).
LAN_SUBNETS=


# #####################################################################################################
# PART 3: your VPN provider (REQUIRED)
# #####################################################################################################
# Find your provider on this list and open its page. It lists exactly which values below it needs:
#   https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers

# ----- VPN_SERVICE_PROVIDER (REQUIRED) ---------------------------------------------------------------
# Your VPN, written exactly as on its Gluetun page, in lowercase. Examples:
#   mullvad, protonvpn, nordvpn, surfshark, private internet access, windscribe, ivpn, airvpn
# Use custom ONLY for a WireGuard VPN that has no Gluetun page; then fill in all six WIREGUARD_ values.
# The stack refuses to start if this is empty.
VPN_SERVICE_PROVIDER=


# ----- VPN_TYPE --------------------------------------------------------------------------------------
# Which VPN protocol to use:
#   openvpn    = fill in the OpenVPN section below.
#   wireguard  = fill in the WireGuard section below. Usually faster. Not every provider supports it.
# Empty means openvpn. Must be wireguard when VPN_SERVICE_PROVIDER is custom.
VPN_TYPE=


# #####################################################################################################
# PART 4a: OpenVPN (only when VPN_TYPE is openvpn or empty; otherwise leave this section empty)
# #####################################################################################################

# ----- OPENVPN_USER and OPENVPN_PASSWORD -------------------------------------------------------------
# Your provider's OpenVPN login. Careful: many providers (NordVPN, Surfshark, ProtonVPN, ...) use a
# separate "service" or "manual setup" username and password for this, NOT the login for their website.
# Your provider's Gluetun page says where to find it.
OPENVPN_USER=
OPENVPN_PASSWORD=


# ----- OPENVPN_PROTOCOL ------------------------------------------------------------------------------
# udp or tcp. Use udp unless your provider or network needs tcp.
# Default: udp
OPENVPN_PROTOCOL=


# #####################################################################################################
# PART 4b: WireGuard (only when VPN_TYPE is wireguard; otherwise leave this section empty)
# #####################################################################################################
# Generate a WireGuard config file on your provider's website (often under "manual setup" or
# "WireGuard configuration") and copy the values from it. A config file looks like:
#   [Interface]
#   PrivateKey = ...        ->  WIREGUARD_PRIVATE_KEY
#   Address = 10.64.12.34/32 ->  WIREGUARD_ADDRESSES
#   DNS = 10.64.0.1         ->  see the DNS section below (add :53)
#   [Peer]
#   PublicKey = ...         ->  WIREGUARD_PUBLIC_KEY      (custom only)
#   PresharedKey = ...      ->  WIREGUARD_PRESHARED_KEY   (only if present)
#   Endpoint = 1.2.3.4:51820 ->  WIREGUARD_ENDPOINT_IP and WIREGUARD_ENDPOINT_PORT (custom only)

# ----- WIREGUARD_PRIVATE_KEY -------------------------------------------------------------------------
# [Interface] PrivateKey. Needed by every provider. Keep it secret.
WIREGUARD_PRIVATE_KEY=


# ----- WIREGUARD_ADDRESSES ---------------------------------------------------------------------------
# [Interface] Address, including the /32, for example 10.64.12.34/32. Needed by most providers.
# If the config lists an IPv4 and an IPv6 address, use only the IPv4 one.
WIREGUARD_ADDRESSES=


# ----- WIREGUARD_PRESHARED_KEY -----------------------------------------------------------------------
# [Peer] PresharedKey. Only if your config file has this line (for example Windscribe). Otherwise empty.
WIREGUARD_PRESHARED_KEY=


# ----- WIREGUARD_PUBLIC_KEY, WIREGUARD_ENDPOINT_IP, WIREGUARD_ENDPOINT_PORT (custom only) -------------
# Only needed when VPN_SERVICE_PROVIDER is custom. Built-in providers fill these in themselves.
#   WIREGUARD_PUBLIC_KEY     = [Peer] PublicKey
#   WIREGUARD_ENDPOINT_IP    = [Peer] Endpoint, the part BEFORE the colon. Must be an IP address, not a
#                              hostname. If your config shows a hostname, look up its IP address.
#   WIREGUARD_ENDPOINT_PORT  = [Peer] Endpoint, the part AFTER the colon, for example 51820.
#                              (Some built-in providers' pages also ask for this one.)
WIREGUARD_PUBLIC_KEY=
WIREGUARD_ENDPOINT_IP=
WIREGUARD_ENDPOINT_PORT=


# #####################################################################################################
# PART 5: DNS (optional, works with OpenVPN and WireGuard)
# #####################################################################################################
# DNS turns names (like your provider's server) into addresses. You have two choices; both keep DNS
# inside the VPN tunnel, so your internet provider never sees your lookups:
#
#   A. Leave both lines empty: Gluetun uses its built-in encrypted DNS (DNS over TLS to Cloudflare).
#
#   B. Use your VPN provider's own DNS server: set BOTH lines below.
#        DNS_UPSTREAM_RESOLVER_TYPE=plain
#        DNS_UPSTREAM_PLAIN_ADDRESSES=10.64.0.1:53
#
# For B, the address MUST end in :53 (the DNS port). Gluetun refuses to start on a bare IP address and
# its log says "not an ip:port". Wrong: 10.64.0.1   Right: 10.64.0.1:53
# The IP is the DNS line of your WireGuard config file, or in your provider's help pages.
# NEVER use your router or any LAN address (like 192.168.1.1:53): that sends every lookup outside the
# VPN tunnel, which is a DNS leak.
#
# With choice B, Gluetun's log shows a warning like:
#   WARN [dns] DNS server address 10.64.0.1 is not in local subnets, make sure to specify it in
#   FIREWALL_OUTBOUND_SUBNETS as 10.64.0.1/32
# Ignore it. Your VPN provider's DNS server is reached through the tunnel, which is what you want.
# Do NOT add that address to LAN_SUBNETS: that would send DNS outside the tunnel, a DNS leak.
# To confirm DNS works, look for "leak check report" and "Public IP address" a few lines later in the log.

# ----- DNS_UPSTREAM_RESOLVER_TYPE --------------------------------------------------------------------
# plain = use the address in DNS_UPSTREAM_PLAIN_ADDRESSES (choice B).
# Empty = Gluetun's encrypted DNS (choice A). Gluetun ignores the address below unless this is plain.
DNS_UPSTREAM_RESOLVER_TYPE=


# ----- DNS_UPSTREAM_PLAIN_ADDRESSES ------------------------------------------------------------------
# Your VPN provider's DNS server as IP:53, for example 10.64.0.1:53. The :53 is required.
# Several servers: separate with commas, for example 10.64.0.1:53,10.64.0.2:53
DNS_UPSTREAM_PLAIN_ADDRESSES=


# #####################################################################################################
# PART 6: server choice (optional)
# #####################################################################################################
# Pick where your VPN connects. Leave all empty to let Gluetun choose. Use the exact names listed on your
# provider's Gluetun page; several values are separated with commas. Not every provider supports every
# option. For streaming, a server close to your IPTV provider's servers usually works best.

# Country, for example Netherlands or United States
SERVER_COUNTRIES=

# Region, for example Europe (the meaning depends on the provider)
SERVER_REGIONS=

# City, for example Amsterdam or New York
SERVER_CITIES=

# One exact server, for example nl-ams-wg-001 (pins the VPN to that server every time)
SERVER_HOSTNAMES=
```

</details>

Everything from the table above applies, plus:

1. **`LAN_SUBNETS`** (required): your LAN, for example `192.168.1.0/24`, passed to Gluetun as
   `FIREWALL_OUTBOUND_SUBNETS`. Without it Gluetun's firewall blocks the LAN and coax cannot reach Channels
   DVR, so the compose file refuses to start without it. It must not overlap the VPN's own address range.
2. **VPN.** Gluetun builds the tunnel from these variables; there are no config files to mount. Set
   `VPN_SERVICE_PROVIDER` (required) and `VPN_TYPE`, then fill in what your provider's page in the
   [provider setup list](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers) asks for, and leave
   the rest empty:
   - **OpenVPN:** usually `OPENVPN_USER` and `OPENVPN_PASSWORD`. Many providers use a separate service login
     for this, not your account login; the provider's page says where to find it.
   - **WireGuard:** usually `WIREGUARD_PRIVATE_KEY` and `WIREGUARD_ADDRESSES` from a WireGuard config you
     generate on the provider's website.
   - **Server choice** (optional): `SERVER_COUNTRIES`, `SERVER_REGIONS`, `SERVER_CITIES` or
     `SERVER_HOSTNAMES`, using the names on the provider's page.
   - **DNS** (OpenVPN and WireGuard): to send DNS to your VPN provider's own DNS server, set
     `DNS_UPSTREAM_RESOLVER_TYPE=plain` and `DNS_UPSTREAM_PLAIN_ADDRESSES` to that server **with the port**, `IP:53` (for
     example `10.255.255.1:53`; Gluetun refuses to start on a bare IP; it is the `DNS` line of a WireGuard config, or in the provider's help pages).
     Lookups then go through the tunnel to the provider, so nothing leaks to your ISP. Never use a LAN or
     router address here; that sends DNS outside the tunnel. Left empty, Gluetun uses its built-in encrypted
     DNS (DNS over TLS to Cloudflare), which also stays inside the tunnel.
     Gluetun then logs `DNS server address ... is not in local subnets, make sure to specify it in
     FIREWALL_OUTBOUND_SUBNETS`. Ignore it: do not add that address to `LAN_SUBNETS`, which would send DNS
     outside the tunnel.
   - **A VPN Gluetun has no page for:** `VPN_SERVICE_PROVIDER=custom`, `VPN_TYPE=wireguard`, and all six
     `WIREGUARD_` values from its WireGuard config (`WIREGUARD_ENDPOINT_IP` must be an IP address). See
     Gluetun's [custom provider page](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/custom.md).
3. **DNS filters.** Gluetun can block domains from lists of malware, tracking and ad hosts. These lists
   can catch IPTV provider, guide and logo servers, so the compose file turns all three off
   (`BLOCK_MALICIOUS`, `BLOCK_SURVEILLANCE`, `BLOCK_ADS`; Gluetun itself turns the malware list on by
   default). To turn one on, add it to `.env`, for example `BLOCK_MALICIOUS=on`.
4. **Ports.** coax shares Gluetun's network, so `HOST_PORT` is published on the `gluetun` service.

Everything else about Gluetun (options, health checks, troubleshooting) is in the
[Gluetun wiki](https://github.com/qdm12/gluetun-wiki).

## First run

1. **Settings.** Set the *Base URL* to the address your DVR uses to reach coax, for example
   `http://192.168.1.50:7780` (use the host's LAN IP, not `localhost`). Set the *Channels DVR URL*, for example
   `http://192.168.1.60:8089`. It is used for Gracenote search and for the stream engine.
2. **Providers.** Add a provider:
   - *Xtream Codes*: the server URL (`http://host:port`) and a username and password.
   - *M3U*: the playlist URL, and optionally an XMLTV URL.

   Press *Test connection*.
3. **Lines.** Add one line per login you have with that provider and set *max streams* to what your plan
   allows (0 means unlimited, for M3U sources without a limit). If you have three logins on one service, add
   three lines; coax spreads tunes across them. Channel list and guide downloads use a line nobody is watching
   when there is one, so they only share a login with a playing stream when every line is in use.
4. **Groups.** coax fetches the provider's live groups. Tick the ones you want (search helps with large
   lists). Only streams in these groups are offered when adding channels. Optionally turn on *auto add* to
   add new streams in those groups as disabled channels at each refresh.
5. **Channels.** Browse and filter the streams, select the ones you want, and add them as channels. Number
   and reorder them, attach backup streams (from any provider), and enable or disable channels.

   **Auto-match** suggests a Gracenote station and backup streams for the channels shown (or the ones you
   selected). Suggestions are only held, never applied for you: they appear under **To review**, sure ones
   already checked, and stay there through reloads, other devices and restarts until you press **Save**, which
   applies only the checked ones, or **Discard**. Unchecked suggestions stay to review after a Save. A channel
   that already has a guide, or that you marked *Left as is*, gets backup suggestions only. The filters above the
   table (*Needs attention*, *Unmatched guide*, *No backups*, *To review*, *Left as is*) match the counters at
   the top; *No backups* counts only backups that can play (not disabled, not vanished from the provider).
6. **Guide.** Each channel uses one guide source, never two:
   - **Gracenote**: coax searches Channels DVR's station database and suggests the best match with a
     confidence score. Channels DVR then uses its own guide data for that channel. Best when it matches.
   - **XMLTV**: use the provider's guide for that channel; pick the XMLTV channel id from the list.
   - **None**: no guide data.

   Picking a Gracenote station clears the channel's XMLTV id, and picking an XMLTV id clears its station.
   Auto-match works the same way: a matched station replaces the XMLTV id.
7. **Lineups.** A default lineup holds all channels. Add more lineups if you want separate sources in the
   DVR (for example kids or sports).
8. **Add each lineup to Channels DVR.** On the *Lineups* page, press **Add to Channels DVR** on a lineup and
   pick:
   - **Guide data**: Gracenote, XMLTV, or Gracenote with XMLTV as the fallback.
   - **Stream limit**: how many channels the DVR may tune at once (coax suggests your providers' total).
   - **Channel numbers**: use coax's numbers, or let Channels DVR number them from a start you choose.
   - **Channel logos**: coax's logos or the guide's.
   - **Source name**: how it appears in the DVR's Sources list.

   coax creates the custom source in Channels DVR for you (MPEG-TS, coax's M3U and XMLTV addresses), loads its
   channels and starts a guide download. You do not need to open Channels DVR's settings.

   **It stays in sync on its own.** After that, every change you make in coax (adding, removing, renaming or
   renumbering channels, new guide matches, logos) is pushed to Channels DVR a few seconds later: coax refreshes
   the source and, when channels or guide ids changed, downloads the guide again. The same happens when a
   provider's channel list refresh renumbers a lineup sorted by provider number. There is no *Refresh* button to
   press in Channels DVR. *Sync now* on the lineup card forces it, and the card shows when it last synced.

   To add a source by hand instead (for example on another DVR), use *Custom Channels* with stream format
   **MPEG-TS**, the source URL `http://<coax-host>:7780/output/m3u/<lineup>.m3u`, and, for XMLTV guide data,
   `http://<coax-host>:7780/output/xmltv/<lineup>.xml`. A source added by hand is not kept in sync.

The *Status* page shows live sessions, viewers, which line each channel holds, refresh results, and the log.

Each playing channel's card lists every stream of the channel in its saved order, with its name, provider,
group and stream id (hover a cut-off name to see it in full). The stream playing is marked *Playing now*;
*Blocked* means Smart Standby is resting it: the pill shows the time left, hovering it shows why, and clicking
it unblocks the stream at once. *Disabled* means you turned it off.

- **Switch** moves playback to that stream right away.
- **Disable** turns a stream off without removing it: it stays listed (dimmed), but coax never tunes it, never
  uses it as a backup and never moves it up. Disabling the stream that is playing moves the channel to the
  next enabled stream at once. Untick it to use the stream again. One stream of each channel always stays
  enabled. The same checkbox is in the channel's stream editor on the *Channels* page.
- **Drag** a stream by its grip (or focus the grip and press Alt with the up or down arrow) to change the
  channel's order. It is saved when you let go and is used from the next failover on; the stream playing now
  keeps playing. Smart Standby still moves a stream that causes real trouble, as it does after any reorder.

Use these when a stream is bad in a way coax cannot see, most often sound off the picture. coax does not
detect that: no check tells a feed whose sound is off from a good one reliably enough to act on. A feed that is
out of sync is almost always built that way by the provider, so it plays the same in any player (try it in VLC)
and tends to stay that way. Disable it and coax will not use it again.

## Settings reference

| Setting | What it does |
| --- | --- |
| Base URL | The address written into the M3U stream links. Must be reachable from the DVR. |
| Channels DVR URL | Your DVR, `http://IP:8089`. Used for Gracenote search and the viewer buffer. |
| Transcode | One fixed format such as `1080p60` (recommended), or `off` to pass streams through (the default). See [GPU transcoding](#gpu-transcoding). |
| Provider refresh hours | How often each provider's catalog is refreshed. A failed refresh keeps the last good catalog. |

## How playback stays smooth

IPTV feeds are uneven. Segments arrive late, a provider stalls, refuses a request, restarts a channel, or
swaps a dead channel for a black "offline" clip. coax's stream engine absorbs this so Channels DVR gets a
steady stream at real time, the way an HDMI encoder delivers one. Every tune runs its own engine process on
one provider line; the engine holds the cushion and the hub keeps the connection alive around it.

### The reserve

- **The cushion lives in coax, not in the players.** On HLS the engine joins about 60 seconds back in the
  provider's playlist (at least 3 segments, or the whole playlist if it is shorter), so you watch about a
  minute behind live. It sends about 4 seconds ahead to Channels DVR and keeps the rest as its reserve. On
  MPEG-TS the reserve is the burst of recent video the provider sends on connect, usually about 20 seconds.
- **When the provider is late,** the reserve keeps video flowing. When it catches up, HLS downloads the
  segments it missed and the reserve refills.
- **Paced by timestamps.** Video leaves coax at exactly real time, timed by each frame's timestamp rather
  than by bytes, including through a switch to a backup with a different bitrate or frame rate.
- **A drained reserve.** If the provider falls behind for longer than the reserve covers, the log shows
  `reserve: empty` (HLS) or `reserve empty` (MPEG-TS), and video arrives only as fast as the provider sends
  it. This is the line that matches visible buffering. With transcoding off, the HLS reserve comes back on
  a retune, and the MPEG-TS reserve through a fresh connection's burst. With transcoding on, coax refills
  it by playing slightly slower; see [Refilling the reserve](#refilling-the-reserve-3-slower).

### Keepalive

Whenever the engine has no video to send for 2 seconds (3 on MPEG-TS), it sends MPEG-TS null packets, which
every player discards, so Channels DVR keeps the connection and a recording keeps going. The log shows
`sending keepalive packets` and then `video flowing again after Ns`. Occasional short gaps are normal.

### Offline placeholders and failover

Two feeds of the same channel are separate encodes that can run minutes apart, and providers do not send the
clock time needed to line them up, so a switch to a backup always jumps forward or back in the program.
coax therefore stays on the feed you are watching through anything it can ride out, and switches only as a
last resort.

| Situation | What coax does |
| --- | --- |
| Late segments, stalls, stale playlists, provider restarts, refused or timed-out requests | Keeps retrying the same feed. On HLS, video that comes back within about a minute is fetched from the playlist, so you continue where you were. Busy answers (429, 503, 509) back off, honoring `Retry-After` up to 30 seconds. |
| Video is lost on HLS: a broken segment is skipped, or an outage outlasts the provider's playlist | The timeline continues across the gap: the picture moves on instead of freezing with silence. |
| The provider's playlist ends (HLS), often right after a burst of proxy errors | Stays on the feed while the reserve lasts, since this is usually a passing hiccup. If the reserve runs low it switches to the next stream without blocking anything. Only an ended playlist that lasts a full minute counts as an offline placeholder, which blocks the stream. |
| The provider redirects to its offline placeholder (MPEG-TS) | Switches to the next stream at once, blocks the stream, and never plays the placeholder. |
| The connection keeps ending after a few seconds (MPEG-TS) | Keeps reconnecting while the reserve lasts; switches to the next stream only when it runs low. Nothing is blocked. |
| No new video for 60 seconds | Switches to the next stream. It switches sooner when the reserve is nearly empty: on HLS after about three segments, on MPEG-TS after about 8 seconds. |
| The feed's audio and video timestamps start more than 3 seconds apart | On HLS, keeps playing it for up to 30 seconds while it tries the next stream, then switches. On MPEG-TS, a backup like this is passed over at connect time. With no other stream it plays anyway. |
| Slower than real time for 5 minutes | Switches to the next stream. On HLS that means below 0.75x while the reserve drains; on MPEG-TS, below 0.85x with the reserve nearly empty. A feed that is slow but keeps the reserve up never triggers a switch. |
| A backup answers busy (429, 503, 509) when coax checks it | Not held against the backup (the provider may still count the stream just left); coax tries the next backup, and asks again a few seconds later if none answered. A backup that is still busy the fourth time in a row is treated as not answering. |
| No backup on the channel | Keeps retrying the same feed until the provider comes back. |
| Sound off its picture with matching timestamps | Not detected. Use **Switch** and **Disable** on the Status page. |

- **Order.** Backups are tried in the order the channel's streams are saved on the *Channels* page.
  Disabled streams are skipped, and streams Smart Standby has blocked are tried last. A stream that failed
  recently, on any channel, is also tried last, both for a new tune and for failover. A failed feed is left
  alone for 1, 2, 4, 8, then 10 minutes, so a channel whose feeds are all down waits instead of hopping between
  dead feeds.
- **Before switching,** coax checks that the backup answers with a real stream, not the placeholder. Once on
  a backup it stays there as long as it works.
- **Across providers.** A backup on another provider is only offered while that provider has a free line,
  and after a switch coax holds that line for the channel. The Status page shows which stream is playing.
- **At the switch,** timestamps continue where the old feed stopped and the new remuxer starts before the
  old one finishes, so the output does not pause.

### Tuner sharing

Channels DVR shares one tuner, one connection to coax, between every device watching the same channel. A
device that joins later starts at the newest video Channels DVR has, so it cannot use a cushion built up in
the first player. coax keeps the cushion on its side and sizes what it hands out to the devices watching:

1. Every 2 seconds the engine asks Channels DVR (`/dvr/activity`) how many devices are reading the channel.
   It finds the channel by looking up coax's M3U in Channels DVR's sources; if that source is out of date,
   coax asks Channels DVR to refresh it, the same as the *Refresh* button.
2. **A device joins:** coax sends an extra 5 seconds out of the reserve at once. Channels DVR passes it to
   every device on the tuner, so the newcomer starts with a cushion and the others gain one too. This
   happens on every join, including a device coming back.
3. **A device leaves:** nothing is taken back, so the devices still watching keep their cushion.
4. **The floor.** On HLS, head starts stop once the reserve is down to about 20 seconds (two 10-second
   segments), which is what riding out a late segment and failing over needs. On MPEG-TS the total head
   start is capped at the burst depth minus 12 seconds, usually 8 to 14 seconds.
5. **The rebuild.** On HLS, once no device has joined for 2 minutes and the reserve is under 30 seconds,
   coax sends video about 1% slower than real time until the reserve is back to 30 seconds. The players'
   extra cushion moves back into the reserve without the output ever pausing. 1% is too small to see or hear.

Example on an HLS channel with 10-second segments:

| What happens | Lead in the players | Reserve in coax |
| --- | --- | --- |
| TV tunes the channel | 4 s | 56 s |
| Laptop joins | 9 s | 51 s |
| Laptop leaves | 9 s | 51 s |
| Laptop joins again | 14 s | 46 s |
| More joins over the evening | up to 40 s | down to the 20 s floor |
| No joins for 2 minutes, reserve under 30 s | eases down by about 1% | rebuilds to 30 s |

Tuner sharing needs the *Channels DVR URL* in Settings. Without it, or if coax cannot find the channel in
Channels DVR, every device gets the single-viewer head start and playback is otherwise the same. Keep tuner
sharing on in Channels DVR.

### Recovery

The hub watches each engine from outside and never gives up while Channels DVR is connected:

- **Engine stopped:** restarted on the same line, resuming its timeline from where it stopped.
- **Engine frozen:** no bytes at all for 25 seconds after video has flowed. coax kills it and starts a new one.
- **No real video for 90 seconds** (only keepalive): coax retunes with a fresh engine. Every second retune
  in a row moves to another free line of the same provider when there is one.
- **Between engines** the hub sends null packets itself, so the connection and any recording stay open.
- **Backoff.** The first restart waits 1 second, and each further restart within 10 minutes doubles the
  wait, up to 15 seconds.

The Status page marks a channel as *Unsteady feed* when its last 10-minute summary shows two or more
switches or 30 seconds or more of keepalive. Each channel's 10-minute summaries are kept in
`data/cache/history/`.


### Smart Standby

Pick the streams for a channel; Smart Standby decides which one plays. It watches every stream of every channel,
switches only on real trouble, and keeps the channel's order arranged so the next tune starts on the stream that
has proven itself. It is built for an appliance you leave alone: it acts only on real trouble, never deletes
anything, and reports every change under the **bell**.

**1. Normal IPTV wobble is ignored.** Stalls the reserve covers, catch-ups, refills and A/V holds are what IPTV
does. Smart Standby rides them out and changes nothing. A shaky stream that coax keeps smooth stays where it is.

**2. Backups are tried best first.** Every stream has a score from its last day: each problem counts (an offline
slate most, then a dead stream, no video, a freeze), older problems fade, and clean playback counts in its favor.
When coax has to leave a stream, it tries the others in score order, least bad first, and blocked streams last.
Before switching it confirms the backup is answering; one that is not is skipped for now. If no backup answers,
coax stays on the current stream and keeps retrying.

**3. Bad streams sit out, they are not deleted.** A stream is blocked (not tuned, last on failover) when:

| Trouble | Blocked |
| --- | --- |
| Offline slate from the provider instead of the channel | At once: 12 hours, then 24 hours, then 3 days |
| It stopped working while another stream of the channel worked | The 3rd time within 6 hours: same ladder |
| coax had to restart or retune it (frozen, or no video for 90 seconds) | Never on its own; it only lowers the stream's score |

The ladder climbs when a stream keeps causing trouble within a week. A stream that stops working while every
other stream of the channel is down too is not blocked, since that is the provider or the network. Neither is a
stream that fails during a burst of the provider's proxy errors (HTTP 407), which hit the whole line, not one
stream. If every
stream of a channel is blocked, coax still plays the least bad one. Click a *Blocked* pill on the Status page to
lift a block early.

**4. The stable stream moves up, once.** When coax leaves a troubled stream for one that plays 10 minutes
cleanly, that stream moves ahead of the troubled one in the channel's saved order. If the troubled one was the
main stream, the stable one becomes the main stream, so the next tune starts on it. A blocked main stream is
replaced the same way, by the stream with the most clean playback. A stream that has never played cleanly never
becomes the main stream.

**5. No trouble, no change.** The order stays put until the next real trouble. A stream that recovers, or whose
block ends, does not move back on its own, so channels do not flip back and forth.

**6. Every change is under the bell.** Each entry says what happened, to which stream and why, with **Undo**
(restores the order and lifts the block), an **X** to dismiss it, and **Clear all**. A channel with one stream is
never changed, and neither is a channel with only one stream left enabled. A disabled stream never becomes the
main stream.

**What Smart Standby cannot see.** A feed whose sound is off its picture plays without errors, so Smart Standby
treats it as healthy. Use **Switch** and **Disable** on the Status page for it (see [First run](#first-run)).

History is kept in `data/cache/feedhealth.json` and the bell's list in `data/notifications.json`.

## HLS or MPEG-TS

Xtream-style providers usually offer each channel two ways:

- **HLS** (`.m3u8`): a playlist of short video files (segments, usually 10 seconds each), with about the
  last minute of them kept available.
- **MPEG-TS** (`.ts`): one continuous stream over a single connection, starting with a short burst of
  recent video.

**Use HLS if your provider offers it.** Set it with *Stream format* on the provider (HLS is the default).
Either way coax sends **MPEG-TS** to Channels DVR, so the DVR source's stream format stays MPEG-TS; only what
coax reads from the provider differs.

| | HLS (recommended) | MPEG-TS |
| --- | --- | --- |
| Reserve | about 56 s (a 60 s join minus the 4 s player head start) | about 20 s (what the provider bursts on connect) |
| Provider hiccup | Missed segments are fetched again from the playlist; nothing is lost if they are still listed (usually about a minute) | Only the reserve covers it; video from an outage longer than the reconnect overlap is lost |
| Reconnects | Continue the same timeline | Usually seamless (the overlap is removed), but the provider can land on a different timeline, which shows as a small skip or repeat |
| Extra head start per joining device | 5 s each, up to about 40 s in total | 5 s each, capped at about 8 to 14 s so failover keeps its headroom |
| Delay behind live | about 60 s | about 20 s |
| Recording extension past the scheduled end | about 105 s | about 65 to 80 s |
| [Smart Standby](#smart-standby): backup confirmed answering before a switch | Yes | Yes |
| Smart Standby: sound off its picture | Not detected; disable the stream on the Status page | Not detected; disable the stream on the Status page |
| Smart Standby: blocking, reordering, new main stream | Yes | Yes |
| Log prefix | `[hlsloop stream]` | `[tsloop stream]` |

- **Xtream providers.** coax builds Xtream stream addresses as
  `http://provider/live/<user>/<pass>/<id>.ts`. With *Stream format* on HLS, the engine reads the same
  channel as `<id>.m3u8` and logs `input: provider stream URL is MPEG-TS; using its HLS form`. That is
  normal.
- **M3U providers.** HLS comes first; MPEG-TS is used only when HLS is not possible. A stream whose URL ends
  in `.m3u8` is always read as HLS. An Xtream-style `/live/` address ending in `.ts` (or with no extension) is
  read through its HLS form while the provider is set to HLS, and as MPEG-TS only when it is set to MPEG-TS.
  Any other `.ts` URL, and the short Xtream form `/<user>/<pass>/<id>` without `/live/` and without an
  extension, has no HLS form and is read as MPEG-TS. Other URLs follow the provider's *Stream format*.
- **Wrong format.** If the HLS engine gets something other than a playlist, the log shows
  `response is not an m3u8 playlist`; set the provider to MPEG-TS. If the MPEG-TS engine gets a playlist, it
  logs `this stream URL is HLS; set the provider format to HLS for it`.

The MPEG-TS engine also:

- **Stitches reconnects.** A reconnect re-sends video that was already sent; coax drops the overlap per
  video frame and resumes on a picture the player can start from. coax finds that picture itself by reading
  the video (H.264 and HEVC), so it does not depend on the provider marking keyframes, which many providers do
  not. If a reconnect lands too far behind to catch up, it replays a short stretch instead of freezing.
- **Refreshes a drifting feed.** A provider that delivers slightly slower than real time for a long time
  gets a fresh connection for a new burst, at most once every 5 minutes (sooner if the reserve is nearly
  empty). This can cause the occasional small skip.
- **Reads floods at playback speed.** An offline channel that serves a recorded clip far faster than real
  time is read at playback speed, with at most 60 seconds queued.

## Recordings

The engine runs a little behind the provider's live edge: that is the reserve. Channels DVR starts and stops
recordings by the clock, so without help the last minute of a program would still be inside coax when the
recording stops, and the ending would be cut off. coax handles this:

- **It extends the recording in Channels DVR,** the same as editing its end time, by the engine's measured
  delay **plus 45 seconds** for the provider itself running behind the broadcast (IPTV providers usually run
  about 20 to 40 seconds behind). That is about 105 seconds for HLS and about 65 to 80 seconds for
  MPEG-TS, never more than 3 minutes.
- **Your padding is kept.** The extension is added on top of whatever end padding the recording or its pass
  already has.
- **Only recordings made through coax.** coax asks Channels DVR which recordings use coax's source and this
  channel. Recordings from any other source (an antenna, another provider, TV Everywhere) are never
  touched, even on the same channel.
- **Once per recording.** Each recording's original length is saved in `data/cache/`, so a restart
  mid-recording does not add the extension twice.
- **Watching while recording is fine.** The stream itself is not changed for a recording.
- **Recordings keep going through problems.** Keepalive packets and the hub's [recovery](#recovery) hold the
  connection open while coax rides out an outage or restarts an engine.

The log line is `recording: <show> on ch N extended by Ns on top of its own padding (...)`, and Channels DVR
shows the later end time. This needs the *Channels DVR URL* in Settings. If your provider runs more than
about 45 seconds behind the broadcast, add a little end padding for those channels yourself.

## GPU transcoding

**Turn transcoding on.** It is off by default only so coax works on any machine out of the box. Unless coax
runs on very weak hardware, set a preset on the *Settings* page. With a GPU the extra load is small (see
[How the GPU is used](#how-the-gpu-is-used)).

Transcoding is what makes coax fully stable, because it is the only way coax can:

- **Keep one format for every feed.** A failover to a backup with a different frame rate or resolution does
  not make Channels DVR (Apple TV in particular) drop frames or drift the audio.
- **Refill the reserve on its own** after a long outage (see [Refilling the reserve](#refilling-the-reserve-3-slower)).

Without it, a backup with a different frame rate can upset the player, and a drained reserve only comes back
on a retune (on MPEG-TS, also through a fresh connection's burst when the provider drifts behind).

The costs are GPU time (or CPU time without a GPU), a little picture quality, and a few seconds more delay
behind live.

coax uses the first encoder that works: Intel or AMD graphics through VAAPI, then NVIDIA NVENC, then the CPU.
The image build fails if its `ffmpeg` lacks `h264_vaapi` or `h264_nvenc`, so both are always present.

### Presets

Pick one on the *Settings* page, under *Transcode*. Every channel leaves coax in that format.

| Preset | Output | Video bitrate |
| --- | --- | --- |
| `off` | passed through untouched (default) | as the provider sends it |
| `1080p60` | 1080p, 59.94 fps | 6 Mbps |
| `1080p30` | 1080p, 29.97 fps | 4.5 Mbps |
| `720p60` | 720p, 59.94 fps | 4.5 Mbps |
| `720p30` | 720p, 29.97 fps | 3 Mbps |
| `1080p50`, `1080p25`, `720p50`, `720p25` | the same for 50 Hz countries | |

- **Which frame rate?** The highest one your channels use. 60 suits sports, news and most US broadcast
  channels; a 30 fps channel just shows each frame twice. 30 is fine for mostly movies and entertainment and
  is easier on weak hardware, but 60 fps sports will look choppier. In 50 Hz countries (Europe, the UK,
  Australia and others) use 50 or 25, since 60 or 30 there makes motion judder.
- **Which size?** 1080p with a GPU, 720p on weak hardware or when encoding on the CPU. The bitrates match what
  IPTV providers typically send.
- Audio is always stereo AAC at 48 kHz, 192 kbps. Interlaced sources are deinterlaced.

### How the GPU is used

With a GPU, the whole transcode stays on it: decoding, deinterlacing, frame rate, scaling and encoding.

- **Intel or AMD (VAAPI):** `-hwaccel vaapi` decoding, `deinterlace_vaapi`, `scale_vaapi`, and `h264_vaapi`.
- **NVIDIA:** CUDA decoding, `bwdif_cuda`, `scale_cuda`, and `h264_nvenc`.
- **CPU:** `libx264` (`veryfast`). Encoding 1080p60 on the CPU takes most of a modern core per stream, and
  small machines cannot keep up; use a 720p preset there.

If GPU decoding fails on a stream (some codecs or profiles), coax decodes that stream on the CPU and keeps
encoding on the GPU, and logs `transcode: GPU decoding failed on this stream, decoding on the CPU instead
(encoding stays on the GPU)`. The image uses Jellyfin's ffmpeg build (8.1.3), which has the full set of
VAAPI and CUDA filters.

### Refilling the reserve (3% slower)

> [!IMPORTANT]
> **With transcoding on, playback runs 3% slower for a while after a provider problem.** If the reserve stays
> below half for 75 seconds on HLS or 60 seconds on MPEG-TS (about 30 s and 10 s; not in the first 90 seconds
> after tuning or switching feeds, and at least a minute after the last change), video and audio play 3%
> slower together, with the audio pitch unchanged, until the reserve is back to three quarters (about 45 s on
> HLS, 16 s on MPEG-TS). The rest of the time video plays at its real speed.

- **Two clean switches per refill.** The encoder is restarted once when the slowdown starts and once when it
  ends, with the speed set from the first frame. Nothing is changed in a running encoder, so audio and video
  cannot drift apart. Each restart begins at the next picture a decoder can start from, keeps the output
  timeline continuous and corrects for where the audio sits in the stream, so the picture does not pause.
- **Works without provider keyframes.** Many providers do not mark keyframes, or start HLS segments in the
  middle of a group of pictures. coax does not rely on either: it finds start points by reading the video
  itself (H.264 and HEVC), and on HLS it works out where the restarted encoder begins from the timestamps it
  has already fed in. If no start point turns up within 10 seconds, it restarts anyway rather than wait.
- **Refill rate:** about 1.8 s of reserve per minute, so a refill takes about 8 to 17 minutes on HLS (up to
  about 26 minutes from empty) and 3 to 5 minutes on MPEG-TS.
- **What you might see:** while it runs, a 60 fps source repeats one frame about twice a second. On a smooth,
  fast camera pan that can show as a tiny hitch.
- **Transcoding only:** without transcoding the audio is copied as is, and slowing it would mean stretching its
  timestamps, which causes crackle or drift.

<details>
<summary><b>Why 3%</b></summary>

The rate is a trade-off between refilling quickly and staying below what people can notice.

- **Motion.** In lab experiments, people comparing two moving patterns side by side can just tell speeds apart
  when they differ by roughly 5 to 10% (McKee, 1981; De Bruyn and Orban, 1988). That is the best case: ideal
  conditions and direct comparison. Judging one speed on its own, as a TV viewer does, is harder. 3% sits below
  the best-case threshold.
- **Speech.** Listeners notice a change in speaking tempo at about 5%, and slowdowns are harder to notice than
  speedups (Quene, 2007). Pitch is corrected, so voices do not drop in pitch.
- **Broadcast precedent.** For decades, 50 Hz countries played 24 fps films at 25 fps, which is 4.2% faster, to
  hundreds of millions of viewers. The faster motion went largely unnoticed; the raised pitch was what people
  noticed, and pitch-corrected versions drew few complaints. Broadcast profanity-delay units such as Eventide's
  BD600 rebuild their delay the same way, at a rate their maker describes as subtle and transparent.
- **Where it can show.** Music is less forgiving: tempo changes of around 3% can be noticed, and trained
  musicians notice smaller changes than other listeners. A second screen showing the same channel in real time
  drifts away by about 1.8 s a minute. And someone who knows the slowdown is on and is watching for it may
  believe they see it; the fair test is a blind comparison.

3% stays under the motion and speech thresholds and under both broadcast precedents, and it refills quickly
enough that a refill is usually over in under 20 minutes. The figures come from the cited studies and are
approximate.

</details>

The log shows `playing 3 percent slower, pitch unchanged` when a refill starts and `back to real speed` when it
ends, and the Status page shows the same.

**Which encoder is in use.** When a transcoding stream starts, the log (Status page, or
`docker logs coax`) shows one of:

```
transcoding with Intel/AMD graphics (VAAPI)
transcoding with NVIDIA graphics (NVENC)
transcoding with the CPU (no hardware encoder found)
```

### Intel

The image includes `intel-media-va-driver-non-free` (Broadwell and newer) and `i965-va-driver` (older
chips). Set `GPU_DEVICE=/dev/dri` in `.env`:

| Variable | Default | Intel or AMD | NVIDIA |
| --- | --- | --- | --- |
| `GPU_DEVICE` | `/dev/null` | `/dev/dri` | leave at the default |
| `DOCKER_RUNTIME` | `runc` | leave at the default | `nvidia` |
| `NVIDIA_VISIBLE_DEVICES` | `all` | leave at the default | `all`, or one GPU's UUID |
| `NVIDIA_DRIVER_CAPABILITIES` | `compute,video,utility` | leave at the default | leave at the default |

coax runs as uid 1000 and adds the group that owns `/dev/dri/renderD128` by itself at start. Check the GPU
from inside the container:

```sh
docker exec coax vainfo --display drm --device /dev/dri/renderD128
```

### AMD

Same as Intel: `GPU_DEVICE=/dev/dri`. The image includes `mesa-va-drivers` (radeonsi). `vainfo` should list
`VAProfileH264*` entries with `VAEntrypointEncSlice`.

### NVIDIA

Install the NVIDIA driver and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host, then set `DOCKER_RUNTIME=nvidia` in `.env`. `NVIDIA_VISIBLE_DEVICES` and
`NVIDIA_DRIVER_CAPABILITIES` already default to every GPU and the capabilities NVENC needs. Check with
`docker exec coax nvidia-smi`.

### Unraid

- **Intel or AMD:** the iGPU driver must be loaded on the host (Intel GPU TOP plugin, or `modprobe i915` /
  `amdgpu` in the go file). In the container template add a *Device* with value `/dev/dri`. If transcoding
  still uses the CPU, add `--group-add=<GID>` to *Extra Parameters*, using the GID from
  `stat -c %g /dev/dri/renderD128` in the Unraid terminal. (Compose Manager users: just use the compose
  file and `.env` above.)
- **NVIDIA:** install the *Nvidia Driver* plugin, then in the template add `--runtime=nvidia` to *Extra
  Parameters*. The image's `NVIDIA_VISIBLE_DEVICES=all` exposes every GPU; to pick one, add the variable
  `NVIDIA_VISIBLE_DEVICES` with the GPU UUID shown in the plugin.
- Map `/data` to a folder under `/mnt/user/appdata/coax`. Ownership is fixed automatically on start.

## Updating

coax is meant to be frozen. Updating is a deliberate step:

1. Read the release notes for the new version on the
   [releases page](https://github.com/mackid1993/coax/releases).
2. Back up `./data` (it is small).
3. On `TAG=latest`, just pull. If you froze a version, change `TAG` in `.env`, for example
   `1.0.3` to `1.1.0`.
4. `docker compose pull && docker compose up -d` (add `-f docker-compose.vpn.yml` for the VPN setup).
5. Check the version at the bottom of the sidebar in the web UI (above the page on a phone, or the first lines of `docker logs coax`).

Gluetun is deliberately not pinned: it is your VPN and firewall, so `docker compose pull` keeps its security fixes current.

## Data folder

Everything coax keeps is in the one bind mount, `./data` on the host, `/data` in the container:

```
data/
  config.json            all settings, providers, lines, channels, lineups (mode 0600, has passwords)
  config.json.bak        the previous config.json, one rolling backup
  cache/
    catalog-<provider>.json   last good catalog for each provider
    xmltv-<lineup>.xml        generated guide for each lineup
    xmltv-ids-<provider>.json XMLTV channel ids offered in the Guide picker
    history/<channel>.jsonl   each channel's 10-minute stream summaries
    hlsloop-jobs-<channel>.json, tsloop-jobs-<channel>.json
                              original length of recordings coax extended; dropped after 24 hours
```

To move coax to another machine, copy the folder. To start over, stop coax and delete `config.json`.
coax runs as uid 1000. At each start the container changes the owner of `/data` to 1000:1000 if anything in
it is owned by someone else. If you run the container with `user:` set, it skips that step, and you have to
make the folder writable yourself (`sudo chown -R 1000:1000 data`).

### Automatic cleanup

coax deletes files it no longer needs so the data folder does not grow forever. The cleanup runs once when
coax starts and then every 24 hours. It only ever touches the files listed here, and only coax's own files:

| What | Where | Deleted when |
| --- | --- | --- |
| Engine state files (`hlsloop-*`, `tsloop-*`), one per tuned channel | `/tmp` inside the container | not written for 24 hours. A playing channel rewrites its file several times a second, so a live stream is never affected. |
| Half-written temporary files (`.tmp*`, `*.tmp`) | `cache/` | older than 24 hours |
| Recording jobs (`hlsloop-jobs-*`, `tsloop-jobs-*`) | `cache/` | older than 24 hours |
| Provider catalog and XMLTV id lists (`catalog-<id>.json`, `xmltv-ids-<id>.json`) | `cache/` | the provider or XMLTV source was deleted, and the file is older than 24 hours |
| Lineup guides (`xmltv-<lineup>.xml`) | `cache/` | the lineup was deleted, and the file is older than 24 hours |
| Channel health history (`history/<channel>.jsonl`) | `cache/history/` | the channel was deleted (after 24 hours), or the file was not written for 30 days |
| Gracenote region index (`gracenote-index-*.json`) | `cache/` | not updated for 30 days; it is rebuilt the next time it is needed |

It never deletes `config.json`, `config.json.bak`, backups (those follow the backup *keep* setting),
or anything that belongs to a provider, lineup or channel you still have. Everything it removes is a cache coax
can rebuild. When it deletes something, the log says `cleanup: removed N old files`. There is no setting for
it.

## Troubleshooting

- **The DVR cannot play a channel.** Check that the Base URL uses the coax host's LAN IP and that the DVR can
  open `http://<coax-host>:7780/healthz` (it returns `ok`).
- **"No free line" or HTTP 503 on tune.** Every line of every stream on that channel is busy. Add a line, raise
  max streams if your plan allows it, or add backup streams from another provider. The Status page shows
  which channel holds each line.
- **Channels flagged as missing after a refresh.** The provider removed or renumbered that stream. Pick a new
  stream for the channel; coax never deletes channels on its own.
- **No guide data.** Gracenote mode needs a correct Channels DVR URL. XMLTV mode needs the XMLTV URL added to
  the DVR source and a guide refresh in coax.
- **Permission denied on `/data`.** See [Data folder](#data-folder).
- **GPU not used.** Look for the `transcoding with ...` line in the log, run `vainfo` or `nvidia-smi` as
  shown under [GPU transcoding](#gpu-transcoding), and check `GPU_DEVICE` or `DOCKER_RUNTIME` in `.env`.
- **VPN: coax cannot reach the DVR.** Check `FIREWALL_OUTBOUND_SUBNETS`.
- **Every tune fails with `response is not an m3u8 playlist`.** The provider sends MPEG-TS; set its *Stream
  format* to MPEG-TS. See [HLS or MPEG-TS](#hls-or-mpeg-ts).
- **Viewer head start or recording extension missing.** Both need the *Channels DVR URL*. The log explains
  lines starting with `viewer buffer: off` or `viewer buffer: waiting`.

### Check that it's working

Tune a channel and open the Status page or the log (`docker logs coax`). Engine lines start with
`[hlsloop stream]` or `[tsloop stream]`.

| | Healthy | Look closer if you see |
| --- | --- | --- |
| HLS | The channel found in Channels DVR, the occasional `catching up N segments`, and a `status:` line every 10 minutes with a full reserve | `reserve: empty` often, repeated `switching to stream`, or `fetch loop stuck` |
| MPEG-TS | `first video received`, and a `status:` line every 10 minutes with a reserve around 15 to 25 s and few errors | `reserve empty` often, repeated `switching to stream`, or a stuck reader |

Common lines:

| Message | Meaning |
| --- | --- |
| `viewer buffer: this stream is Channels DVR <source> ch N` | coax found this channel in Channels DVR. |
| `viewer buffer: N viewers on Channels DVR ch N, sent the new viewer a Ns head start` | Another device joined. `reserve at its floor` (HLS) or `head start dropped` (MPEG-TS) means there was not room for the full 5 seconds. |
| `viewer buffer: Channels DVR source <source> is out of date, refreshing it` | The DVR's copy of coax's M3U was stale, so coax refreshed it. |
| `catching up N segments` / `playlist stale Ns, re-resolving` | The provider stalled; the reserve covered it. |
| `provider restarted the channel, rejoining at the live edge` | The provider restarted the channel. |
| `...; switching to stream <id>` | Failover to that stream (coax names streams by their stream id). |
| `stream <id> failed; not using it again for Ns` | A feed failed and is skipped for that long. |
| `audio and video are Ns apart in this feed; holding it back` | The provider sent audio and video out of step. |
| `skipped segment N` | A segment could not be downloaded in time. |
| `stream error (...); reconnect N in Ns` | MPEG-TS: the connection dropped. Normal once in a while. |
| `engine sent nothing for 25s; restarting it on the same line` | The hub restarted a frozen engine. |
| `no video for 90s; retuning ...` | The hub retuned a stream that carried only keepalive. |
| `status: ...` | A summary every 10 minutes: reserve, retries, switches and keepalive time. |

### What coax can't fix

- **A switch to a backup jumps forward or back in the program.** Feeds of the same channel can run minutes
  apart. A provider that restarts a channel can cause the same jump without any switch.
- **A provider outage longer than the reserve** on a channel with no working backup. The picture freezes;
  when the provider comes back coax recovers on its own, but video from the outage is lost.
- **Feeds with a different resolution, frame rate or codec** can cause a brief flicker at a failover while
  the player's decoder resets, and a different frame rate can make some players (Channels DVR on Apple TV)
  drop frames until you tune again. Transcoding hides this.
- **A drained reserve** only comes back on a retune unless transcoding is on (MPEG-TS can also top it up with
  a fresh connection).
- **A bad feed that keeps running,** for example audio slightly out of sync. coax cannot detect it on its
  own; press **Switch** on another stream of the channel on the Status page, and **Disable** the bad one.
- **Problems after coax,** such as a device's Wi-Fi dropping out for longer than its head start.
- **Connection limits.** Providers often count a stream as active for a short time after the last request,
  so a failover or retune can briefly look like two connections. If every connection on your account is
  busy at that moment, the switch may have to wait.
- **MPEG-TS only:** video from an outage longer than the reconnect overlap is lost, a reconnect onto a
  different provider timeline shows a small skip or repeat, and the cushion is about 20 s instead of 60 s.

## Building from source

```sh
scripts/build-image.sh 1.0.6-1       # stamps the version and builds coax:1.0.6-1
python -m unittest discover tests    # tests run on Python 3.11 or newer
```

`scripts/stamp-version.sh <version>` writes the version into `app/VERSION`; `scripts/build-image.sh` runs it
and then `docker build`. Without a version it stamps `dev-<commit>`, and a plain `docker build` without a stamp
shows `dev`. The version appears at the bottom of the sidebar in the web UI (above the page on a phone) and in the first log lines.

Every build input is pinned: the `python:3.13-slim-trixie` base image by digest, Debian packages from a fixed
[snapshot.debian.org](https://snapshot.debian.org/) date (`DEBIAN_SNAPSHOT` in the Dockerfile), Python
packages by exact version and SHA-256 (`requirements.txt`, installed with `--require-hashes`), and GitHub
Actions by commit SHA.

## Credits

- The stream engine (`app/engine/hlsloop.py`, `app/engine/tsloop.py`) comes from
  [mackid1993/channels-dvr-iptv-tools](https://github.com/mackid1993/channels-dvr-iptv-tools),
  where it ran in production as Dispatcharr stream profiles.
- The tuner handoff to Channels DVR (`app/hub.py`: one connection, one engine, one line slot; copy with a
  flush after every chunk; stop the engine before freeing its slot) and the one-click "Add to Channels DVR"
  and source refresh (`app/channels_dvr.py`) are adapted from
  [ah4c (Android HDMI for Channels)](https://github.com/sullrich/ah4c) by sullrich and contributors,
  ported from its Go code (`main.go`, `m3uintegration.go`). ah4c is released under an MIT-style license,
  Copyright 2023 Fancy Bits, LLC.
- Provider ingest, group selection, connection limits and refresh follow the behavior of
  [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).
- [Gluetun](https://github.com/qdm12/gluetun) by qdm12 (MIT), used unmodified as a separate container.
- [FFmpeg](https://ffmpeg.org/), [Requests](https://requests.readthedocs.io/) and
  [cryptography](https://cryptography.io/).

## Updates

coax is built to need as few updates as possible. It is meant to be set up once and left alone, so new
releases are rare on purpose. A long gap since the last release does not mean the project is abandoned;
it is still used and maintained.

## Contributing

coax is provided as is. Issues, bug reports, feature requests and pull requests are not accepted. Forks and remixes are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE). Copyright (c) 2026 mackid1993.
