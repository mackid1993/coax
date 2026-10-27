# coax

coax is a small, self-hosted IPTV appliance that works with [Channels DVR](https://getchannels.com/dvr-server/).
You give it your IPTV playlists (Xtream Codes logins or M3U URLs). It gives your DVR a steady tuner stream for
each channel, an M3U with Gracenote station IDs, and an optional XMLTV guide.

It does one job and is built once: no auto-update, no code downloaded at runtime, no telemetry, no database.
Everything you set lives in one folder, `./data`.

> coax is an independent project. It is not affiliated with, endorsed by, or sponsored by Fancy Bits, LLC,
> the makers of Channels and Channels DVR. "Channels DVR" is used here only to describe what coax works with.

> **LAN only.** coax has no login and no authentication. Your provider passwords are in its config. Never
> expose port 7780 to the internet (no port forwarding, no public reverse proxy). Use it on your home network,
> or reach it remotely through a VPN such as Tailscale or WireGuard.

## Features

- **Xtream Codes and M3U providers**, several side by side, with test connection and scheduled refresh.
- **Group selection** like Dispatcharr: pick which provider groups to bring in from a searchable checklist.
- **Connection pools**: several lines (logins) per provider, each with its own max streams. A tune takes the
  first stream of the channel that has a free line, across providers.
- **Backup streams per channel**, mixed across providers, with automatic failover in the stream engine.
- **Tuner sharing**: a second viewer of the same channel shares the running stream and uses no extra line.
- **Gracenote guide data** through Channels DVR's own station search, with one-click suggestions, or
  **XMLTV** from your provider, or both.
- **Optional transcoding** to one fixed format (1080p or 720p, 60/30/50/25 fps) on Intel or AMD graphics
  (VAAPI), NVIDIA (NVENC), or the CPU.
- **Optional VPN** for provider traffic using Gluetun and your own WireGuard config.
- A polished web UI with a live Status page: sessions, viewers, line usage, throughput, and the log.

## Quick start

You need Docker with Docker Compose on a machine on the same network as your Channels DVR server.

### Without a VPN

```sh
mkdir coax && cd coax
curl -fLO https://raw.githubusercontent.com/mackid1993/coax/main/docker-compose.yml
docker compose up -d
```

Open `http://<this-host>:7780`.

### With a VPN (Gluetun + WireGuard)

```sh
mkdir -p coax/data/wireguard && cd coax
curl -fLO https://raw.githubusercontent.com/mackid1993/coax/main/docker-compose.vpn.yml
# copy your VPN provider's WireGuard config to ./data/wireguard/wg0.conf
# edit FIREWALL_OUTBOUND_SUBNETS in docker-compose.vpn.yml to match your LAN
docker compose -f docker-compose.vpn.yml up -d
```

Open `http://<this-host>:7780`. See [VPN setup](#vpn-setup) for details.

Both compose files pin a released version, for example `ghcr.io/mackid1993/coax:1.0.0`. The `:main` tag is
built from every commit on the main branch and is for testing only.

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
   - **Both**: send the Gracenote station id and the XMLTV guide.
   - **None**: no guide data.
7. **Lineups.** A default lineup holds all channels. Add more lineups if you want separate sources in the
   DVR (for example kids or sports).
8. **Add coax to Channels DVR.** In the Channels DVR web UI, open *Settings*, *Sources*, add a *Custom
   Channels* source, and fill in:
   - Stream format: **MPEG-TS**
   - Source URL: `http://<coax-host>:7780/output/m3u/<lineup>.m3u` (for example `.../output/m3u/main.m3u`)
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
| Transcode | `off` (pass through, default) or one fixed format such as `1080p60`. See [GPU](#gpu-transcoding). |
| Join behind (seconds) | How far back in the buffer a new viewer of an already running channel starts. |
| Channel shutdown delay | Seconds a channel keeps running after the last viewer leaves, for quick re-tunes. |
| Provider refresh hours | How often each provider's catalog is refreshed. A failed refresh keeps the last good catalog. |

## GPU transcoding

Transcoding is off by default and most setups do not need it. When on, coax uses the first encoder that works:
Intel or AMD graphics through VAAPI, then NVIDIA NVENC, then the CPU.

**Intel or AMD (VAAPI).** The image includes `intel-media-va-driver-non-free` (amd64 only), `mesa-va-drivers`
and `vainfo`. In your compose file uncomment `devices` and `group_add`:

```yaml
    devices:
      - /dev/dri:/dev/dri
    group_add:
      - "105"   # the host's render group GID
      - "44"    # the host's video group GID
```

coax runs as the non-root user `coax` (uid 1000), so it needs the host's `render` group (and on some systems
`video`) to open `/dev/dri/renderD128`. Find the numbers on the host with `getent group render video` or
`stat -c '%g' /dev/dri/renderD128`. Check the GPU from inside the container:

```sh
docker exec coax vainfo --display drm --device /dev/dri/renderD128
```

**NVIDIA.** Install the NVIDIA Container Toolkit on the host and give the container the GPU, for example:

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu, video]
```

Debian's `ffmpeg` build is used as is; if NVENC is not available in it on your system, coax falls back to the
CPU and says so in the log.

## VPN setup

`docker-compose.vpn.yml` runs [Gluetun](https://github.com/qdm12/gluetun) (MIT license, by qdm12), pinned by
digest, and puts coax in Gluetun's network so all of coax's traffic (provider logins, playlists, streams) goes
through your VPN.

1. Get a WireGuard config file from your VPN provider and save it as `./data/wireguard/wg0.conf`. The
   `Endpoint` must be an IP address, not a hostname.
2. Set `FIREWALL_OUTBOUND_SUBNETS` to your LAN (for example `192.168.1.0/24`). Gluetun's firewall blocks
   everything outside the tunnel otherwise, and coax could not reach Channels DVR. The subnet must not
   overlap the VPN's own address range.
3. The coax port (7780) is published on the `gluetun` service, because coax shares its network.
4. If the tunnel drops, Gluetun blocks traffic rather than leaking it, and streams stop until it reconnects.

Gluetun needs `NET_ADMIN` and `/dev/net/tun`, both already in the compose file.

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
  wireguard/
    wg0.conf             only with docker-compose.vpn.yml
```

To move coax to another machine, copy the folder. To start over, stop coax and delete `config.json`.
The container runs as uid 1000; if you create `./data` yourself, make sure that user can write to it
(`sudo chown -R 1000:1000 data`).

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
- **Permission denied on `/data`.** See the ownership note under [Data folder](#data-folder).
- **GPU not used.** Run `vainfo` as shown under [GPU transcoding](#gpu-transcoding) and check the `group_add`
  GIDs.
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
  [mackid1993/channels-dvr-stationid-matcher](https://github.com/mackid1993/channels-dvr-stationid-matcher),
  where it ran in production as Dispatcharr stream profiles.
- Tuner sharing and group handling follow the behavior of [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr).
- [Gluetun](https://github.com/qdm12/gluetun) by qdm12 (MIT), used unmodified as a separate container.
- [FFmpeg](https://ffmpeg.org/) and [Requests](https://requests.readthedocs.io/).

## License

[MIT](LICENSE). Copyright (c) 2026 mackid1993.
