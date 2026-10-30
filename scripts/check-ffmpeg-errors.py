#!/usr/bin/env python3
"""Check that ffmpeg still prints the error messages the engines read to tell a GPU decoding failure from an
audio input or filter that failed (gpufail in app/engine/hlsloop.py and tsloop.py).

Each case makes ffmpeg fail the way an encoder can at tune time, on the CPU (no GPU needed), and checks the
engines' verdict on its messages. An ffmpeg upgrade that rewords one of them fails this check instead of quietly
moving streams to CPU decoding (or keeping a failing GPU decoder). The image build runs it against the image's
own ffmpeg.

usage: scripts/check-ffmpeg-errors.py [ffmpeg command...]   (default: ffmpeg; CI: docker exec -i coax ffmpeg)"""
import ast
import os
import re
import subprocess
import sys

ENGINES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "engine")
Q = ["-hide_banner", "-nostdin", "-loglevel", "error"]

# (what it stands for, ffmpeg arguments, arguments of an ffmpeg whose output is the input, GPU fallback expected)
CASES = (
    ("an audio input that matches no stream",
     Q + ["-f", "lavfi", "-i", "sine=d=1", "-filter_complex", "[0:i:9999]anull[ao]", "-map", "[ao]", "-f", "null", "-"],
     None, False),
    ("an audio filter that cannot start",
     Q + ["-f", "lavfi", "-i", "sine=d=1", "-filter_complex", "[0:a]surround=chl_out=bogus[ao]", "-map", "[ao]",
          "-f", "null", "-"],
     None, False),
    ("an audio decoder that cannot open",
     ["-hide_banner", "-loglevel", "error", "-c:a", "vorbis", "-i", "pipe:0", "-f", "null", "-"],
     Q + ["-f", "lavfi", "-i", "sine=d=1", "-c:a", "aac", "-f", "mpegts", "pipe:1"], False),
    ("a GPU device that cannot be created",
     Q + ["-init_hw_device", "vaapi=va:/dev/dri/renderD999", "-f", "lavfi", "-i", "testsrc=d=1", "-f", "null", "-"],
     None, True),
    ("GPU decoding without a device",
     Q + ["-hwaccel", "vaapi", "-hwaccel_device", "/dev/dri/renderD999", "-f", "lavfi", "-i", "testsrc=d=1",
          "-f", "null", "-"],
     None, True),
)


def verdict(name):
    """gpufail from an engine, with what it needs, run on a list of messages."""
    tree = ast.parse(open(os.path.join(ENGINES, name + ".py")).read())
    want = {"FEL", "FET", "GPUERR", "AUDERR", "gpufail"}
    nodes = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in want) or
             (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in want for t in n.targets))]
    ns = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), name + ".py", "exec"), ns)

    def judge(lines):
        ns["FEL"][0] = lines[-20:]   # the engines keep an encoder's last 20 messages
        return ns["gpufail"](type("P", (), {"pid": 0})())
    return judge


def main():
    ff = sys.argv[1:] or ["ffmpeg"]
    judges = {name: verdict(name) for name in ("hlsloop", "tsloop")}
    bad = 0
    for what, args, feed, want in CASES:
        data = subprocess.run(ff + feed, stdin=subprocess.DEVNULL, capture_output=True).stdout if feed else None
        r = subprocess.run(ff + args, input=data, stdin=None if feed else subprocess.DEVNULL, capture_output=True)
        lines = [s.rstrip() for s in r.stderr.decode("utf-8", "replace").splitlines() if s.strip()]
        got = {name: judge(lines) for name, judge in judges.items()}
        if r.returncode == 0:
            print("FAIL %s: ffmpeg no longer fails here; update this case" % what)
            bad += 1
        elif any(g != want for g in got.values()):
            print("FAIL %s: GPU fallback %s, expected %s; ffmpeg said:" % (what, got, want))
            print("\n".join("    " + s for s in lines[-20:]))
            bad += 1
        else:
            print("ok   %s (GPU fallback: %s)" % (what, "yes" if want else "no"))
    if bad:
        print("ffmpeg's messages changed: update GPUERR/AUDERR in app/engine/hlsloop.py and tsloop.py (and the "
              "GpuFallbackTest cases in tests/test_engine_adapt.py)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
