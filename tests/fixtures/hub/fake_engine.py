"""Fake stream engine for hub tests.

Writes MPEG-TS-shaped packets (sync byte 0x47 then a 4-byte big-endian packet
counter) to stdout at a fixed rate, logs one stderr line, and can misbehave on
request. Usage:

    fake_engine.py URL USER_AGENT CHANNEL_ID [--rate BYTES_PER_SEC] [--exit-after SECONDS]
                   [--exit-code N] [--ignore-term] [--junk N] [--odd-writes]
"""

import argparse
import os
import signal
import struct
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument("url")
parser.add_argument("user_agent")
parser.add_argument("channel_id")
parser.add_argument("--rate", type=int, default=188 * 1000)
parser.add_argument("--exit-after", type=float, default=0.0)
parser.add_argument("--exit-code", type=int, default=1)
parser.add_argument("--ignore-term", action="store_true")
parser.add_argument("--junk", type=int, default=0, help="bytes of garbage before the first packet")
parser.add_argument("--odd-writes", action="store_true", help="write in sizes that split packets")
parser.add_argument("--freeze-after", type=float, default=0.0, help="stop writing (but stay alive) after N seconds")
parser.add_argument("--nulls-only", action="store_true", help="send only MPEG-TS null packets (no video)")
args = parser.parse_args()

if args.ignore_term:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

sys.stderr.write("fake engine up for %s channel %s\n" % (args.url, args.channel_id))
sys.stderr.flush()

out = sys.stdout.buffer
if args.junk:
    out.write(b"\x00" * args.junk)
counter = 0
start = time.monotonic()
tick = 0.02
per_tick = max(1, int(args.rate * tick) // 188)
pending = b""
try:
    while True:
        if args.exit_after and time.monotonic() - start >= args.exit_after:
            out.flush()
            os._exit(args.exit_code)
        if args.freeze_after and time.monotonic() - start >= args.freeze_after:
            time.sleep(3600)
        if args.nulls_only:
            data = (b"\x47\x1f\xff\x10" + b"\xff" * 184) * per_tick
        else:
            data = b"".join(b"\x47" + struct.pack(">I", counter + i) + b"\xff" * 183 for i in range(per_tick))
        counter += per_tick
        if args.odd_writes:
            data = pending + data
            cut = len(data) - 77
            data, pending = data[:cut], data[cut:]
        out.write(data)
        out.flush()
        time.sleep(tick)
except (BrokenPipeError, KeyboardInterrupt):
    pass
