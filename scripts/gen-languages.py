#!/usr/bin/env python3
"""Build app/engine/languages.json from the ISO 639-2 code list of the Library of Congress
(https://www.loc.gov/standards/iso639-2/ISO-639-2_utf-8.txt, lines "B|T|alpha-2|English|French").

usage: scripts/gen-languages.py ISO-639-2_utf-8.txt

The most widely spoken languages come first (the order of the Settings list), then every other language A to Z.
Special codes (undetermined, multiple, uncoded, no content, local use) are left out: a track tagged with one of
them simply never matches a language, so the main track plays."""
import json
import os
import sys

TOP = ["eng", "zho", "hin", "spa", "ara", "fra", "ben", "por", "rus", "urd", "ind", "deu", "jpn", "mar", "tel",
       "tur", "tam", "vie", "tgl", "kor", "fas", "hau", "swa", "jav", "ita", "pan", "guj", "tha", "kan", "amh",
       "pol", "ukr", "msa", "mya", "nld", "ron", "uzb", "heb", "ell", "hun", "ces", "swe", "srp", "hrv", "bul"]
SPECIAL = {"und", "mul", "mis", "zxx", "qaa-qtz"}

rows = []
with open(sys.argv[1], encoding="utf-8-sig") as f:
    for line in f:
        b, t, a2, en = (line.rstrip("\n").split("|") + [""] * 5)[:4]
        if not b or b in SPECIAL:
            continue
        code = t or b
        rows.append({"code": code, "name": en.split(";")[0].strip(),
                     "aliases": sorted({x for x in (b, t, a2) if x and x != code})})
by = {r["code"]: r for r in rows}
missing = [c for c in TOP if c not in by]
assert not missing, missing
out = [by[c] for c in TOP] + sorted((r for r in rows if r["code"] not in TOP), key=lambda r: r["name"].lower())
path = os.path.join(os.path.dirname(__file__), "..", "app", "engine", "languages.json")
with open(path, "w", encoding="utf-8") as f:
    json.dump({"source": "ISO 639-2, Library of Congress", "top": len(TOP), "languages": out}, f,
              ensure_ascii=False, separators=(",", ":"))
    f.write("\n")
print(len(out), "languages")
