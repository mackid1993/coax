<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/coax-wordmark-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/brand/coax-wordmark-light.svg">
    <img alt="coax" src="assets/brand/coax-wordmark-light.svg" height="72">
  </picture>
</h1>

> **Not affiliated with Fancy Bits.** coax is an independent, unofficial project. It is not affiliated with, endorsed by, sponsored by, or supported by Fancy Bits, LLC or Channels DVR. Fancy Bits and Channels DVR will not provide support for coax; please do not contact them about it. Channels DVR is a trademark of Fancy Bits, LLC. All other trademarks and product names are the property of their respective owners and are used here only to describe compatibility.

coax is designed specifically for [Channels DVR](https://getchannels.com/dvr-server/). It is a small,
self-hosted IPTV appliance: you give it your IPTV playlists (Xtream Codes logins or M3U URLs), and it gives
your DVR a steady tuner stream for each channel, an M3U with Gracenote station IDs, and an optional XMLTV
guide.

It does one job and is built once: no auto-update, no code downloaded at runtime, no telemetry, no database.
Everything you set lives in one folder, `./data`.

> **LAN only.** coax has no login and no authentication. Your provider passwords are in its config. Never
> expose port 7780 to the internet (no port forwarding, no public reverse proxy). Use it on your home network,
> or reach it remotely through a VPN such as Tailscale or WireGuard.

## Contents

- [Features](#features)
- [Run it without a VPN](#run-it-without-a-vpn)
- [Run it with a VPN (Gluetun)](#run-it-with-a-vpn-gluetun)
- [First run](#first-run)
- [Settings reference](#settings-reference)
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
- **Gracenote guide data** through Channels DVR's own station search, with one-click suggestions, or
  **XMLTV** from your provider.
- **Optional transcoding** to one fixed format (1080p or 720p, 60/30/50/25 fps) on Intel or AMD graphics
  (VAAPI), NVIDIA (NVENC), or the CPU.
- **Optional VPN** for all provider traffic using Gluetun.
- A web UI with a live Status page: sessions, viewers, line usage, throughput, and the log.

**User agent.** coax always identifies itself to providers as `TiviMate/5.1.6 (Android 12)`. This is
hard-coded and the same for every provider; it is not a setting.

## Run it without a VPN

You need Docker with Docker Compose on a machine on the same network as your Channels DVR server.

```sh
mkdir coax && cd coax
curl -fLO https://raw.githubusercontent.com/mackid1993/coax/main/docker-compose.yml
docker compose up -d
```

Open `http://<this-host>:7780` and continue with [First run](#first-run).

Docker creates `./data` on first start. If it ends up owned by root, coax fixes that itself: the container
starts as root only long enough to `chown` `/data` to uid 1000, then runs coax as uid 1000.

The compose file pins a released version, for example `ghcr.io/mackid1993/coax:1.0.0`. The `:main` tag is
built from every commit on the main branch and is for testing only.

## Run it with a VPN (Gluetun)

`docker-compose.vpn.yml` runs [Gluetun](https://github.com/qdm12/gluetun) (MIT license, by qdm12), pinned by
digest, and puts coax in Gluetun's network, so all of coax's traffic (provider logins, playlists, guides,
streams) goes through your VPN. If the tunnel drops, Gluetun blocks traffic rather than leaking it, and
streams stop until it reconnects.

```sh
mkdir -p coax && cd coax
curl -fLO https://raw.githubusercontent.com/mackid1993/coax/main/docker-compose.vpn.yml
# create .env with your WireGuard values (see below), then: chmod 600 .env
# edit FIREWALL_OUTBOUND_SUBNETS in docker-compose.vpn.yml to match your LAN
docker compose -f docker-compose.vpn.yml up -d
```

What to set:

1. **VPN settings.** Gluetun is set up entirely with environment variables and builds the tunnel itself;
   there are no config files to mount. The compose file has two examples; use one:
   - **WireGuard** (active by default): Gluetun's custom provider, which works with any WireGuard VPN. Put
     `WG_ENDPOINT_IP`, `WG_ENDPOINT_PORT`, `WG_PUBLIC_KEY`, `WG_PRIVATE_KEY`, `WG_ADDRESSES`, and
     `WG_PRESHARED_KEY` if your provider uses one, in a `.env` file next to the compose file. See Gluetun's
     [custom provider page](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/custom.md).
   - **OpenVPN** (commented out): a built-in provider with your VPN login. Comment out the WireGuard block,
     uncomment the OpenVPN one, and put `VPN_PROVIDER`, `OPENVPN_USER`, `OPENVPN_PASSWORD` and
     `SERVER_COUNTRIES` in `.env`.
   Keep `.env` private (`chmod 600 .env`).
   To use one of Gluetun's built-in providers instead (Mullvad, ProtonVPN, and many more), replace the
   `gluetun` environment with the settings from your provider's page in the
   [provider setup list](https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers).
2. **`FIREWALL_OUTBOUND_SUBNETS`.** Set it to your LAN, for example `192.168.1.0/24`. Gluetun's firewall
   blocks everything outside the tunnel otherwise, and coax could not reach Channels DVR. It must not
   overlap the VPN's own address range.
3. **Ports.** coax shares Gluetun's network, so port 7780 is published on the `gluetun` service, not on
   `coax`.

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
   three lines; coax spreads tunes across them.
4. **Groups.** coax fetches the provider's live groups. Tick the ones you want (search helps with large
   lists). Only streams in these groups are offered when adding channels. Optionally turn on *auto add* to
   add new streams in those groups as disabled channels at each refresh.
5. **Channels.** Browse and filter the streams, select the ones you want, and add them as channels. Number
   and reorder them, attach backup streams (from any provider), and enable or disable channels.
6. **Guide.** For each channel choose a guide mode:
   - **Gracenote**: coax searches Channels DVR's station database and suggests the best match with a
     confidence score. Channels DVR then uses its own guide data for that channel. Best when it matches.
   - **XMLTV**: use the provider's guide for that channel; pick the XMLTV channel id from the list.
   - **Both**: the Gracenote station id, with XMLTV as the fallback for channels that have none.
   - **None**: no guide data.
7. **Lineups.** A default lineup holds all channels. Add more lineups if you want separate sources in the
   DVR (for example kids or sports).
8. **Add coax to Channels DVR.** In the Channels DVR web UI, open *Settings*, *Sources*, add a *Custom
   Channels* source, and fill in:
   - Stream format: **MPEG-TS**
   - Source URL: `http://<coax-host>:7780/output/m3u/<lineup>.m3u` (for example `.../output/m3u/main.m3u`).
     Use the URL exactly as shown, without a path-rewriting proxy in between.
   - XMLTV guide data (only if you use XMLTV or Both): `http://<coax-host>:7780/output/xmltv/<lineup>.xml`
   - Stream limit: the total of your lines' max streams, or leave it unlimited.

   The exact menu names can differ between Channels DVR versions. Channels with a Gracenote station id map to
   Channels DVR guide data automatically.

The *Status* page shows live sessions, viewers, which line each channel holds, refresh results, and the log.

## Settings reference

| Setting | What it does |
| --- | --- |
| Base URL | The address written into the M3U stream links. Must be reachable from the DVR. |
| Channels DVR URL | Your DVR, `http://IP:8089`. Used for Gracenote search and the viewer buffer. |
| Transcode | `off` (pass through, default) or one fixed format such as `1080p60`. See [GPU transcoding](#gpu-transcoding). |
| Provider refresh hours | How often each provider's catalog is refreshed. A failed refresh keeps the last good catalog. |

## GPU transcoding

Transcoding is off by default and most setups do not need it. When on, coax uses the first encoder that works:
Intel or AMD graphics through VAAPI, then NVIDIA NVENC, then the CPU. The image build fails if its `ffmpeg`
lacks `h264_vaapi` or `h264_nvenc`, so both are always present.

**Which encoder is in use.** When a transcoding stream starts, the log (Status page, or
`docker logs coax`) shows one of:

```
transcoding with Intel/AMD graphics (VAAPI)
transcoding with NVIDIA graphics (NVENC)
transcoding with the CPU (no hardware encoder found)
```

### Intel

The image includes `intel-media-va-driver-non-free` (Broadwell and newer) and `i965-va-driver` (older
chips), both amd64 only. Uncomment `devices` in your compose file:

```yaml
    devices:
      - /dev/dri:/dev/dri
```

coax runs as uid 1000 and adds the group that owns `/dev/dri/renderD128` by itself at start. If it still
falls back to the CPU, add that group explicitly with `group_add`; get the number on the host with
`stat -c %g /dev/dri/renderD128`. Check the GPU from inside the container:

```sh
docker exec coax vainfo --display drm --device /dev/dri/renderD128
```

### AMD

Same as Intel: pass `/dev/dri` (and `group_add` if needed). The image includes `mesa-va-drivers` (radeonsi)
on every architecture. `vainfo` should list `VAProfileH264*` entries with `VAEntrypointEncSlice`.

### NVIDIA

Install the NVIDIA driver and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host, then uncomment the `deploy` block in your compose file:

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu, video, compute, utility]
```

The image already sets `NVIDIA_VISIBLE_DEVICES=all` and `NVIDIA_DRIVER_CAPABILITIES=compute,video,utility`.
Check with `docker exec coax nvidia-smi`.

### Unraid

- **Intel or AMD:** the iGPU driver must be loaded on the host (Intel GPU TOP plugin, or `modprobe i915` /
  `amdgpu` in the go file). In the container template add a *Device* with value `/dev/dri`. If transcoding
  still uses the CPU, add `--group-add=<GID>` to *Extra Parameters*, using the GID from
  `stat -c %g /dev/dri/renderD128` in the Unraid terminal.
- **NVIDIA:** install the *Nvidia Driver* plugin, then in the template add `--runtime=nvidia` to *Extra
  Parameters*. The image's `NVIDIA_VISIBLE_DEVICES=all` exposes every GPU; to pick one, add the variable
  `NVIDIA_VISIBLE_DEVICES` with the GPU UUID shown in the plugin.
- Map `/data` to a folder under `/mnt/user/appdata/coax`. Ownership is fixed automatically on start.

## Updating

coax is meant to be frozen. Updating is a deliberate step:

1. Read the release notes for the new version on the
   [releases page](https://github.com/mackid1993/coax/releases).
2. Back up `./data` (it is small).
3. Change the version in your compose file, for example `ghcr.io/mackid1993/coax:1.0.0` to `:1.1.0`.
   Do not use `latest`; coax never publishes it.
4. `docker compose pull && docker compose up -d` (add `-f docker-compose.vpn.yml` for the VPN setup).

Gluetun is pinned too. Update it the same way when you choose to, by changing its tag and digest.

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
```

To move coax to another machine, copy the folder. To start over, stop coax and delete `config.json`.
coax runs as uid 1000. At each start the container changes the owner of `/data` to 1000:1000 if anything in
it is owned by someone else. If you run the container with `user:` set, it skips that step, and you have to
make the folder writable yourself (`sudo chown -R 1000:1000 data`).

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
  shown under [GPU transcoding](#gpu-transcoding), and check `/dev/dri`, `group_add`, or the NVIDIA runtime.
- **VPN: coax cannot reach the DVR.** Check `FIREWALL_OUTBOUND_SUBNETS`.

## Building from source

```sh
docker build -t coax:local .
python -m unittest discover tests    # tests run on Python 3.11 or newer
```

Every build input is pinned: the `python:3.13-slim-trixie` base image by digest, Debian packages from a fixed
[snapshot.debian.org](https://snapshot.debian.org/) date (`DEBIAN_SNAPSHOT` in the Dockerfile), Python
packages by exact version and SHA-256 (`requirements.txt`, installed with `--require-hashes`), and GitHub
Actions by commit SHA.

## Credits

- The stream engine (`app/engine/hlsloop.py`, `app/engine/tsloop.py`) comes from
  [mackid1993/channels-dvr-iptv-tools](https://github.com/mackid1993/channels-dvr-iptv-tools),
  where it ran in production as Dispatcharr stream profiles.
- Tuner sharing and group handling follow the behavior of [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).
- [Gluetun](https://github.com/qdm12/gluetun) by qdm12 (MIT), used unmodified as a separate container.
- [FFmpeg](https://ffmpeg.org/), [Requests](https://requests.readthedocs.io/) and
  [cryptography](https://cryptography.io/).

## License

[MIT](LICENSE). Copyright (c) 2026 mackid1993.
