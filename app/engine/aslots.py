"""Audio inputs: the provider's audio fed to the transcoder so that ffmpeg never sees an audio format change.

ffmpeg opens its audio decoders once, when it starts, and rebuilds its audio filters whenever the decoded format
changes. A provider switching codec mid-stream (an AAC ad in a Dolby show) leaves the decoder unable to read the
audio (silence), and a switch between 5.1 and stereo (every ad break) makes ffmpeg rebuild its filters, which
can drop a fraction of a second of sound when several inputs are mixed. So ffmpeg never sees either:

- The program table (PMT) lists one fixed audio input per exact format (Dolby Digital 5.1 at 48 kHz, AAC stereo
  at 44.1 kHz, ...; SLOTS) on fixed PIDs, in place of the provider's audio tracks. ffmpeg decodes each and mixes
  them (amix, normalize=0) into the one output format.
- The provider track that should play (choose()) is cut into frames (aframes), and each frame goes to the input
  of its exact format, re-wrapped with its own timestamp. Every other track is dropped.
- Every other input gets silent frames of its own format on the same clock, so no decoder or filter ever sees a
  change and the mix never waits. When no provider audio is playing at all, every input gets silence on the
  video's clock.

A codec switch, a 5.1/stereo ad, the audio moving to another track, a failover to a feed with other audio: the
audio simply arrives in another input. No restart, no filter rebuild, no change in what leaves coax.
"""
import base64
import json
import os

import aframes

# (family, name, one silent frame of that exact format)
_TABLE = (
    ("ac3", "Dolby Digital 2.0",
     "C3fvBwxAQ+EG8AEICAghAQEEVx8+fPnz58+fPnz7/nV8+fPnz58+fPj/nV8+fPnz58+fPjL/Hs2bIAAAAAAAHju7u7u7u4AA"
     "AAAAAAAAAAAAAA1rWtaAAAAAAHd3d3d3dwAAAAAAAAAAAAAAAAwAAAAAAAADx3d3d3d3cAAAAAAAAAAAAAAAAa1rWtAAAAAA"
     "Du7u7u7u4AAAAAAAAAAAAAAAAYAAAAAAAAB47u7u7u7uAAAAAAAAAAAAAAAANa1rWgAAAAAB3d3d3d3cAAAAAAAAAAAAAAAA"
     "MAAAAAAAAA8d3d3d3d3AAAAAAAAAAAAAAAAGta1rQAAAAAA7u7u7u7uAAAAAAAAAAAAAAAAGAAAAAAAAAeO7u7u7u7gAAAAA"
     "AAAAAAAAAADWta1oAAAAAAd3d3d3d3AAAAAAAAAAAAAAAADAAAAAAAAAPHd3d3d3dwAAAAAAAAAAAAAAABrWta0AAAAAAO7u"
     "7u7u7gAAAAAAAAAAAAAAAAAAAAAAAFK2"),
    ("ac3", "Dolby Digital 5.1",
     "C3e3PBxA6/hAPv90hAQQgIIQEEICCEBAqrx8+fPnz58+/51fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+"
     "fPnz58+fPnz58+P+dXz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPj/nV8+fPnz58+fPn"
     "z58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz4/51fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+"
     "fPnz58+fPnz58+fPnz58+P+dXz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPj/nXL/BEi"
     "RIkSJEgAAAAAbXz58+bbbbbePHjx48ePHjx48ePG223z5rWta1rWta1rWtAAAADfPnz5ttttt48ePHjx48ePHjx4222+fNa1"
     "rWta1rWta1oAAAAb58+fNttttvHjx48ePHjx48ePHjbbb581rWta1rWta1rWgAAABtfPnz5ttttt48ePHjx48ePHjx4222+f"
     "Na1rWta1rWta1oAAAAb58+fNttttvHjx48ePHjx48ePG7bbb581rWta1rWta1rWgAAABsgfAAAAAAAABtfPnz5ttttt48ePH"
     "jx48ePHjx48bbbfPmta1rWta1rWta0AAAAN8+fPm22223jx48ePHjx48ePHjbbb581rWta1rWta1rWgAAABvnz58222228eP"
     "Hjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG18+fPm22223jx48ePHjx48ePHjbbb581rWta1rWta1rWgAAABvnz5822222"
     "8ePHjx48ePHjx48btttvnzWta1rWta1rWtaAAAAGyB8AAAAAAAAG18+fPm22223jx48ePHjx48ePHjxttt8+a1rWta1rWta1"
     "rQAAAA3z58+bbbbbePHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG+fPnzbbbbbx48ePHjx48ePHjx4222+fNa1rWta1rW"
     "ta1oAAAAbXz58+bbbbbePHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG+fPnzbbbbbx48ePHjx48ePHjxu222+fNa1rWta"
     "1rWta1oAAAAbIHwAAAAAAAAbXz58+bbbbbePHjx48ePHjx48ePG223z5rWta1rWta1rWtAAAADfPnz5ttttt48ePHjx48ePH"
     "jx4222+fNa1rWta1rWta1oAAAAb58+fNttttvHjx48ePHjx48ePHjbbb581rWta1rWta1rWgAAABtfPnz5ttttt48ePHjx48"
     "ePHjx4222+fNa1rWta1rWta1oAAAAb58+fNttttvHjx48ePHjx48ePG7bbb581rWta1rWta1rWgAAABsgfAAAAAAAABtfPnz"
     "5ttttt48ePHjx48ePHjx48bbbfPmta1rWta1rWta0AAAAN8+fPm22223jx48ePHjx48ePHjbbb581rWta1rWta1rWgAAABvn"
     "z58222228ePHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG18+fPm22223jx48ePHjx48ePHjbbb581rWta1rWta1rWgAAA"
     "Bvnz58222228ePHjx48ePHjx48btttvnzWta1rWta1rWtaAAAAGyB8AAAAAAAAG18+fPm22223jx48ePHjx48ePHjxttt8+a"
     "1rWta1rWta1rQAAAA3z58+bbbbbePHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG+fPnzbbbbbx48ePHjx48ePHjx4222+"
     "fNa1rWta1rWta1oAAAAbXz58+bbbbbePHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG+fPnzbbbbbx48ePHjx48ePHjxu2"
     "22+fNa1rWta1rWta1oAAAAbIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4q"),
    ("ac3", "Dolby Digital mono",
     "C3eQMQhAL4QpQ/51fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPjL/BEAAAAA2vnz58222228ePHjx48ePHjx48"
     "btttvnzWtSgAAAAAbXz58+bbbbbePHjx48ePHjx48eN2223z5rWpQAAAAANr58+fNttttvHjx48ePHjx48ePG7bbb581rUoA"
     "AAAAG18+fPm22223jx48ePHjx48ePHjdttt8+a1qUAAAAADa+fPnzbbbbbx48ePHjx48ePHjxu222+fNa1KAAAAABtfPnz5t"
     "tttt48ePHjx48ePHjx43bbbfPmtakAAAAAAAAAAAAAAAAAAAAABLCg=="),
    ("ac3", "Dolby Digital 5.0",
     "C3f3fBxA6vhAPv90hAQQgIIQEEICCEBAqrj58+fPnz59/zq+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58"
     "+fPnz58+fPnz58f86vnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fH/Or58+fPnz58+fP"
     "nz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnx/zq+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58"
     "+fPnz58+fPnz58+fPnz58f86vnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fGX+CJEiRI"
     "kSAAAAABtfPnz5ttttt48ePHjx48ePHjx48bbbfPmta1rWta1rWta0AAAAN8+fPm22223jx48ePHjx48ePHjbbb581rWta1r"
     "Wta1rWgAAABvnz58222228ePHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG18+fPm22223jx48ePHjx48ePHjbbb581rWt"
     "a1rWta1rWgAAABvnz58222228ePHjx48ePHjx48btttvnzWta1rWta1rWtYD4AAAAAAAAbXz58+bbbbbePHjx48ePHjx48eP"
     "G223z5rWta1rWta1rWtAAAADfPnz5ttttt48ePHjx48ePHjx4222+fNa1rWta1rWta1oAAAAb58+fNttttvHjx48ePHjx48e"
     "PHjbbb581rWta1rWta1rWgAAABtfPnz5ttttt48ePHjx48ePHjx4222+fNa1rWta1rWta1oAAAAb58+fNttttvHjx48ePHjx"
     "48ePG7bbb581rWta1rWta1rWA+AAAAAAAAG18+fPm22223jx48ePHjx48ePHjxttt8+a1rWta1rWta1rQAAAA3z58+bbbbbe"
     "PHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG+fPnzbbbbbx48ePHjx48ePHjx4222+fNa1rWta1rWta1oAAAAbXz58+bbb"
     "bbePHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG+fPnzbbbbbx48ePHjx48ePHjxu222+fNa1rWta1rWta1gPgAAAAAAAB"
     "tfPnz5ttttt48ePHjx48ePHjx48bbbfPmta1rWta1rWta0AAAAN8+fPm22223jx48ePHjx48ePHjbbb581rWta1rWta1rWgA"
     "AABvnz58222228ePHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG18+fPm22223jx48ePHjx48ePHjbbb581rWta1rWta1r"
     "WgAAABvnz58222228ePHjx48ePHjx48btttvnzWta1rWta1rWtYD4AAAAAAAAbXz58+bbbbbePHjx48ePHjx48ePG223z5rW"
     "ta1rWta1rWtAAAADfPnz5ttttt48ePHjx48ePHjx4222+fNa1rWta1rWta1oAAAAb58+fNttttvHjx48ePHjx48ePHjbbb58"
     "1rWta1rWta1rWgAAABtfPnz5ttttt48ePHjx48ePHjx4222+fNa1rWta1rWta1oAAAAb58+fNttttvHjx48ePHjx48ePG7bb"
     "b581rWta1rWta1rWA+AAAAAAAAG18+fPm22223jx48ePHjx48ePHjxttt8+a1rWta1rWta1rQAAAA3z58+bbbbbePHjx48eP"
     "Hjx48eNttvnzWta1rWta1rWtaAAAAG+fPnzbbbbbx48ePHjx48ePHjx4222+fNa1rWta1rWta1oAAAAbXz58+bbbbbePHjx4"
     "8ePHjx48eNttvnzWta1rWta1rWtaAAAAG+fPnzbbbbbx48ePHjx48ePHjxu222+fNa1rWta1rWta1gAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAI5+"),
    ("eac3", "Dolby Digital Plus 2.0",
     "C3cAvzSHwAAgAAAARwAABAQEAQEBAY+fPnz58+fPnz59/zq+fPnz58+fPnx/zq+fPnz58+fPnwAAAAAAB3jx48d3d3d3AAAA"
     "AAAAAAAADWta1rWta+fAAAAAA7x48eO7u7u7gAAAAAAAAAAAAAAAAAADvHjx47u7u7uAAAAAAAAAAAAGta1rWta18+AAAAAB"
     "3jx48d3d3d3AAAAAAAAAAAAAAAAAAAHePHjx3d3d3cAAAAAAAAAAAANa1rWta1r58AAAAADvHjx47u7u7uAAAAAAAAAAAAAA"
     "AAAAAO8ePHju7u7u4AAAAAAAAAAAAa1rWta1rXz4AAAAAHePHjx3d3d3cAAAAAAAAAAAAAAAAAAAd48ePHd3d3dwAAAAAAAA"
     "AAAA1rWta1rWvnwAAAAAO8ePHju7u7u4AAAAAAAAAAAAAAAAAAA7x48eO7u7u7gAAAAAAAAAAABrWta1rWtfPgAAAAAd48eP"
     "Hd3d3dwAAAAAAAAAAAAAAAAAAAAAABqv"),
    ("eac3", "Dolby Digital Plus 5.1",
     "C3cC/z+HwAAgAAAAAgAAAAQQH7oEBAEBAEBAEBAEBBj58+fPnz59/zq+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz5"
     "8+fPnz58+fPnz58+fPnz58f86vnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fH/Or58+f"
     "Pnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnx/zq+fPnz58+fPnz58+fPnz58+fPnz58+fPnz5"
     "8+fPnz58+fPnz58+fPnz58+fPnz58f86vnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fPnz58+fH"
     "/OoAAAAANr58+fNttttvHjx48ePHjx48ePHjbbb581rWta1rWta1rWgAAABvnz58222228ePHjx48ePHjx48bbbfPmta1rWt"
     "a1rWta0AAAAN8+fPm22223jx48ePHjx48ePHjxttt8+a1rWta1rWta1rQAAAA2vnz58222228ePHjx48ePHjx48bbbfPmta1"
     "rWta1rWta0AAAAN8+fPm22223jx48ePHjx48ePHjdttt8+a1rWta1rWta1rQAAAA2QAAAAADa+fPnzbbbbbx48ePHjx48ePH"
     "jx4222+fNa1rWta1rWta1oAAAAb58+fNttttvHjx48ePHjx48ePG223z5rWta1rWta1rWtAAAADfPnz5ttttt48ePHjx48eP"
     "Hjx48bbbfPmta1rWta1rWta0AAAANr58+fNttttvHjx48ePHjx48ePG223z5rWta1rWta1rWtAAAADfPnz5ttttt48ePHjx4"
     "8ePHjx43bbbfPmta1rWta1rWta0AAAANkAAAAAA2vnz58222228ePHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG+fPnzb"
     "bbbbx48ePHjx48ePHjxttt8+a1rWta1rWta1rQAAAA3z58+bbbbbePHjx48ePHjx48ePG223z5rWta1rWta1rWtAAAADa+fP"
     "nzbbbbbx48ePHjx48ePHjxttt8+a1rWta1rWta1rQAAAA3z58+bbbbbePHjx48ePHjx48eN2223z5rWta1rWta1rWtAAAADZ"
     "AAAAAANr58+fNttttvHjx48ePHjx48ePHjbbb581rWta1rWta1rWgAAABvnz58222228ePHjx48ePHjx48bbbfPmta1rWta1"
     "rWta0AAAAN8+fPm22223jx48ePHjx48ePHjxttt8+a1rWta1rWta1rQAAAA2vnz58222228ePHjx48ePHjx48bbbfPmta1rW"
     "ta1rWta0AAAAN8+fPm22223jx48ePHjx48ePHjdttt8+a1rWta1rWta1rQAAAA2QAAAAADa+fPnzbbbbbx48ePHjx48ePHjx"
     "4222+fNa1rWta1rWta1oAAAAb58+fNttttvHjx48ePHjx48ePG223z5rWta1rWta1rWtAAAADfPnz5ttttt48ePHjx48ePHj"
     "x48bbbfPmta1rWta1rWta0AAAANr58+fNttttvHjx48ePHjx48ePG223z5rWta1rWta1rWtAAAADfPnz5ttttt48ePHjx48e"
     "PHjx43bbbfPmta1rWta1rWta0AAAANkAAAAAA2vnz58222228ePHjx48ePHjx48eNttvnzWta1rWta1rWtaAAAAG+fPnzbbb"
     "bbx48ePHjx48ePHjxttt8+a1rWta1rWta1rQAAAA3z58+bbbbbePHjx48ePHjx48ePG223z5rWta1rWta1rWtAAAADa+fPnz"
     "bbbbbx48ePHjx48ePHjxttt8+a1rWta1rWta1rQAAAA3z58+bbbbbePHjx48ePHjx48eN2223z5rWta1rWta1rWtAAAADZAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAFgG"),
    ("aac", "AAC stereo 48 kHz",
     "//FMgAG//CEQBGCMHA=="),
    ("aac", "AAC stereo 44.1 kHz",
     "//FQgAG//CEQBGCMHA=="),
    ("aac", "AAC 5.1",
     "//FNgANf/AEYIAEIgCMEYCMQBGCMDCMAAOA="),
    ("aac", "AAC mono",
     "//FMQAF//AEYIAc="),
    ("aac", "HE-AAC stereo 48 kHz",
     "//FYgCuf/CEQA0BoG7QEKgAAAAAAAAAAAAAAN/+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAG8hAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADg"),
    ("aac", "HE-AAC stereo 44.1 kHz",
     "//FcgC9f/CEQA0BoG3QEKgAAAAAAAAAAAAA3/4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAb0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAADg"),
    ("aac", "HE-AAC v2",
     "//FYQBZf/ADQIAadAoAAAAAAARgN8aAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHA=="),
    ("mp2", "MPEG audio stereo",
     "//2EBEQyIiIRMyERERERJJJSJAAAAAAAAACqqqqqqqqqqvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvvnve973d22222+fNa222"
     "+a1rWta1rWta1r581r3ve97u7bbbbfPmtbbbfNa1rWta1rWta18+a173ve93dttttvnzWtttvmta1rWta1rWta+fNa973ve7"
     "u22223z5rW223zWta1rWta1rWtfPmte973vd3bbbbb581rbbb5rWta1rWta1rWvnzWve973u7ttttt8+a1ttt81rWta1rWta"
     "1rXz5rXve973d22222+fNa222+a1rWta1rWta1r581r3ve97u7bbbbfPmtbbbfNa1rWta1rWta18+a173ve93dttttvnzWtt"
     "tvmta1rWta1rWta+fNa973ve7u22223z5rW223zWta1rWta1rWtfPmte973vd3bbbbb581rbbb5rWta1rWta1rWvnzWve973"
     "u7ttttt8+a1ttt81rWta1rWta1rXz5rQ"),
    ("mp2", "MPEG audio mono",
     "//1ExEMiEhEREkogAAAAqqqqqvvvvvvvvvvvvvvvvnve7u23zXzWta1rWvmve93dtvmvmta1rWtfNe97u7bfNfNa1rWta+a9"
     "73d22+a+a1rWta18173u7tt8181rWta1r5r3vd3bb5r5rWta1rXzXve7u23zXzWta1rWvmve93dtvmvmta1rWtfNe97u7bfN"
     "fNa1rWta+a973d22+a+a1rWta18173u7tt8181rWta1r5r3vd3bb5r5rWta1rXzQ"),
    ("latm", "AAC (LATM)",
     "VuAeIAARkB/gvvAQAmMLsxmxkXGRwXGJgZACEQBGCMHA"),
    ("dts", "DTS 5.1",
     "f/6AAfw8P/J14AU4AAnve97///+AAAA22ySSAAH/gAADbbbbbbbbbYAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAD3uuuXXXvezM2ZuqcAAAHvddcuuve9mZszdU4AAAPe665dde97MzZm6pwAAAe911y66972ZmzN1TgAAA97rrl1173szNmb"
     "qnAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAECBAgQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAgQIEAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAQIECBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAECBAgQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAgQIEAA"
     "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACkkkkkkkkiIiIiIiIiIgAAAAAAAAAAIiIiIiIiIiIkkkkkkkkkkkkVVVVV"
     "VVVVVWqqqqqAAAAAAAAAAAAAAAAAAAAAJJJJJJJJIiIiIiIiIiIAAAAAAAAAACIiIiIiIiIiJJJJJJJJJJJJFVVVVVVVVVVq"
     "qqqqgAAAAAAAAAAAAAAAAAAAACSSSSSSSSIiIiIiIiIiAAAAAAAAAAAiIiIiIiIiIiSSSSSSSSSSSRVVVVVVVVVVaqqqqoAA"
     "AAAAAAAAAAAAAAAAAAAkkkkkkkkiIiIiIiIiIgAAAAAAAAAAIiIiIiIiIiIkkkkkkkkkkkkVVVVVVVVVVWqqqqqAAAAAAAAA"
     "AAAAAAAAAAAAJJJJJJJJIiIiIiIiIiIAAAAAAAAAACIiIiIiIiIiJJJJJJJJJJJJFVVVVVVVVVVqqqqqgAAAAAAAAAAAAAAA"
     "AAAAACSSSSSSSSIiIiIiIiIiAAAAAAAAAAAiIiIiIiIiIiSSSSSSSSSSSRVVVVVVVVVVaqqqqoAAAAAAAAAAAAAAAAAAAAAk"
     "kkkkkkkiIiIiIiIiIgAAAAAAAAAAIiIiIiIiIiIkkkkkkkkkkkkVVVVVVVVVVWqqqqqAAAAAAAAAAAAAAAAAAAAAJJJJJJJJ"
     "IiIiIiIiIiIAAAAAAAAAACIiIiIiIiIiJJJJJJJJJJJJFVVVVVVVVVVqqqqqgAAAAAAAAAAAAAAAAAAAACSSSSSSSSIiIiIi"
     "IiIiAAAAAAAAAAAiIiIiIiIiIiSSSSSSSSSSSRVVVVVVVVVVaqqqqoAAAAAAAAAAAAAAAAAAAAAkkkkkkkkiIiIiIiIiIgAA"
     "AAAAAAAAIiIiIiIiIiIkkkkkkkkkkkkVVVVVVVVVVWqqqqqAAAAAAAAAAAAAAAAAAAAAP//AAAAAAAAAAAAAAAAAAAAAAAAA"
     "AAAAAAAAAAAAAAAAAAAAAA=="),
)
FAMS = ("ac3", "eac3", "aac", "latm", "mp2", "dts")
STYPE = {"ac3": 0x81, "eac3": 0x87, "aac": 0x0F, "latm": 0x11, "mp2": 0x03, "dts": 0x82}
SIDS = {"ac3": 0xBD, "eac3": 0xBD, "dts": 0xBD, "aac": 0xC0, "latm": 0xC0, "mp2": 0xC0}
WRAP = 1 << 33
LAG = 45000            # silence on the video clock trails the picture by 0.5 s
GAP = 90000            # no frame from the playing track for 1 s of video: every input gets silence
JUMP = 90000 * 10      # a clock jump (provider restart, wrap): an input's silence restarts there
MAXPES = 60000

SLOTS = []             # [{fam, name, pid, frame, key, dur}]
for _i, (_fam, _name, _b64) in enumerate(_TABLE):
    _f = base64.b64decode(_b64)
    _size, _key, _samples, _rate = aframes.frame(_fam, _f, 0)
    SLOTS.append({"fam": _fam, "name": _name, "pid": 0x1FC0 + _i, "frame": _f, "key": _key,
                  "dur": _samples * 90000 // _rate})
PIDS = tuple(s["pid"] for s in SLOTS)
BYKEY = {s["key"]: i for i, s in enumerate(SLOTS)}
SPARE = {}             # a format no input has exactly: the family's first input (ffmpeg rebuilds once there)
for _i, _s in enumerate(SLOTS):
    SPARE.setdefault(_s["fam"], _i)


def dif(a, b):
    """a - b in 90 kHz ticks across the 33-bit wrap."""
    return (a - b + WRAP // 2) % WRAP - WRAP // 2


def _languages():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "languages.json"), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    out = {}
    for lang in data.get("languages") or []:
        for c in [lang["code"]] + lang.get("aliases", []):
            out[c.lower()] = lang["code"]
    return out


ALIASES = _languages()
SAME = {"fil": "tgl", "nob": "nor", "nno": "nor"}   # codes providers use for the same spoken language


def canon(code):
    """A language tag as its ISO 639-2 code ("spa" for "es", "fra" for "fre"), or "" when it names no language."""
    c = (code or "").strip().lower()
    c = ALIASES.get(c, c if len(c) == 3 and c.isalpha() else "")
    if c in ("und", "mul", "mis", "zxx") or "qaa" <= c <= "qtz":
        return ""
    return SAME.get(c, c)


def family(st, desc):
    """The audio family of an elementary stream (stream type and descriptors), or None when it is not audio
    coax can take."""
    tags = {}
    k = 0
    while k + 2 <= len(desc):
        tags.setdefault(desc[k], desc[k + 2:k + 2 + desc[k + 1]])
        k += 2 + desc[k + 1]
    reg = tags.get(5, b"")[:4]
    if st == 0x81 or (st == 6 and (0x6A in tags or reg == b"AC-3")):
        return "ac3"
    if st == 0x87 or (st == 6 and (0x7A in tags or reg == b"EAC3")):
        return "eac3"
    if st == 0x0F:
        return "aac"
    if st == 0x11:
        return "latm"
    if st in (0x03, 0x04):
        return "mp2"
    if st in (0x82, 0x85, 0x86) or (st == 6 and (0x7B in tags or reg in (b"DTS1", b"DTS2", b"DTS3"))):
        return "dts"
    return None


def describe(desc):
    """(language, main) from an audio track's descriptors. main is False for audio description, hearing impaired,
    commentary and similar extra tracks."""
    lang, main = "", True
    k = 0
    while k + 2 <= len(desc):
        tag, n = desc[k], desc[k + 1]
        b = desc[k + 2:k + 2 + n]
        if tag == 0x0A and n >= 4:                       # ISO 639 language: code + audio_type
            lang = lang or b[:3].decode("latin-1")
            if b[3] in (2, 3):
                main = False
        elif tag == 0x7F and n >= 2 and b[0] == 6:       # DVB supplementary audio: editorial classification
            if (b[1] >> 2) & 31:
                main = False
            if b[1] & 1 and n >= 5:
                lang = b[2:5].decode("latin-1")
        elif tag in (0x6A, 0x7A) and n >= 2 and b[0] & 0x80:   # DVB AC-3 / E-AC-3: component type
            if (b[1] >> 3) & 7:
                main = False
        elif tag == 0x81 and n >= 3:                     # ATSC AC-3: bit stream mode
            if b[2] >> 5:
                main = False
        k += 2 + n
    return canon(lang), main


AC3CH = (2, 1, 2, 3, 3, 4, 4, 5)


def chans(key):
    """Channels of a frame format key (aframes), 0 when unknown."""
    if not key:
        return 0
    if key[0] in ("ac3", "eac3"):
        return AC3CH[key[1]] + key[2]
    if key[0] == "aac":
        return (0, 1, 2, 3, 4, 5, 6, 8)[key[3]] if key[3] < 8 else 0
    if key[0] == "mp2":
        return 1 if key[4] else 2
    return 6 if key[0] == "dts" else 2


def _crc(data):
    crc = 0xFFFFFFFF
    for x in data:
        crc ^= x << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7 if crc & 0x80000000 else crc << 1) & 0xFFFFFFFF
    return crc


class Feeder:
    """Rewrites one engine's transcoder input; see the module docstring. pref: preferred language ("" for the
    provider's main track). Not thread safe: call it from the writer only."""

    def __init__(self, pref="", log=None):
        self.pref = canon(pref)
        self.log = log or (lambda m: None)
        self.pat = b""
        self.pmt_pid = None
        self.sec = None             # PMT section being assembled
        self.pmt_out = b""          # our PMT, packetized
        self.pmt_cc = 0
        self.tracks = []            # [{pid, fam, lang, main, order}] in the provider's order
        self.fam = {}               # provider audio pid -> family
        self.cc = [0] * len(SLOTS)
        self.end = [None] * len(SLOTS)   # where each input's audio ends, 90 kHz
        self.said_keys = set()
        self.reset()

    def reset(self):
        """A new stream (failover, hand switch): its clock and tracks start over."""
        self.end = [None] * len(SLOTS)
        self.seen, self.ch = {}, {}
        self.vclk = self.aclk = self.va = None
        self.cur = self.pend = None
        self.up = [None, 0]
        self.said = None
        self.t0 = None              # the first audio PTS heard on this stream
        self.pre = True             # a new encoder: its inputs start where its first audio or picture does
        self._drop()

    def _drop(self):
        self.buf = bytearray()      # the playing track's bytes not yet cut into frames
        self.marks = []             # [(offset in buf, PTS)] where its PES packets began
        self.npts = None            # the next frame's PTS when its PES carried none
        self.grp = None             # [input, first PTS, [frames], duration] waiting to be sent
        self.dep = None             # the input of the last independent E-AC-3 frame

    def psi(self):
        """For the first bytes a new encoder gets: PAT and our PMT (it may start mid-segment, before the next
        table), so ffmpeg knows every input at once. The inputs' silence starts again from the audio that
        follows: what went to the old encoder must not put the new one's audio ahead of its first picture."""
        self.end = [None] * len(SLOTS)
        self.grp = None
        self.aclk = self.va = None
        self.pre = True
        return self.pat + self.pmt_out

    def audio_pids(self):
        return set(self.fam)

    # -- output packets ---------------------------------------------------------------------------------
    def _packets(self, pid, pay, s=None):
        out = []
        first = True
        for i in range(0, len(pay), 184):
            chunk = pay[i:i + 184]
            if s is None:
                cc = self.pmt_cc
                self.pmt_cc = (cc + 1) & 15
            else:
                cc = self.cc[s]
                self.cc[s] = (cc + 1) & 15
            h = bytes([0x47, (0x40 if first else 0) | pid >> 8, pid & 0xFF])
            first = False
            if len(chunk) < 184:
                st = 184 - len(chunk)
                out.append(h + bytes([0x30 | cc, st - 1]) + (b"\x00" + b"\xff" * (st - 2) if st > 1 else b"") + chunk)
            else:
                out.append(h + bytes([0x10 | cc]) + chunk)
        return b"".join(out)

    def _pes(self, s, pts, data):
        p = pts % WRAP
        n = 8 + len(data)
        head = bytes([0, 0, 1, SIDS[SLOTS[s]["fam"]], n >> 8 if n < 65536 else 0, n & 0xFF if n < 65536 else 0,
                      0x80, 0x80, 5, 0x21 | (p >> 29) & 0x0E, (p >> 22) & 0xFF, 0x01 | (p >> 14) & 0xFE,
                      (p >> 7) & 0xFF, 0x01 | (p << 1) & 0xFE])
        return self._packets(SLOTS[s]["pid"], head + data, s)

    def _fill(self, s, upto):
        """Silent frames in input s up to upto."""
        e = self.end[s]
        if e is None or abs(dif(upto, e)) > JUMP:
            self.end[s] = upto % WRAP
            return b""
        d = SLOTS[s]["dur"]
        n = dif(upto, e) // d
        out = []
        while n > 0:
            k = min(n, 30)
            out.append(self._pes(s, e, SLOTS[s]["frame"] * k))
            e += k * d
            n -= k
        self.end[s] = e % WRAP
        return b"".join(out)

    def _fill_all(self, upto, but=None):
        return b"".join(self._fill(x, upto) for x in range(len(SLOTS)) if x != but)

    # -- program table ----------------------------------------------------------------------------------
    def _pmt(self, sec):
        """Our PMT from the provider's: its video and other tracks, its audio replaced by the inputs."""
        if len(sec) < 16 or sec[0] != 2:
            return
        pil = (sec[10] & 15) << 8 | sec[11]
        keep = bytearray()
        tracks, fam = [], {}
        j = 12 + pil
        while j + 5 <= len(sec) - 4:
            st, pid, il = sec[j], (sec[j + 1] & 31) << 8 | sec[j + 2], (sec[j + 3] & 15) << 8 | sec[j + 4]
            desc = bytes(sec[j + 5:j + 5 + il])
            f = family(st, desc)
            if f is not None:
                lang, main = describe(desc)
                tracks.append({"pid": pid, "fam": f, "lang": lang, "main": main, "order": len(tracks)})
                fam[pid] = f
            elif pid not in PIDS:
                keep += sec[j:j + 5 + il]   # video, subtitles, data and audio coax cannot take stay as they are
            j += 5 + il
        if [(t["pid"], t["fam"], t["lang"], t["main"]) for t in tracks] != \
                [(t["pid"], t["fam"], t["lang"], t["main"]) for t in self.tracks]:
            self.log("audio tracks: " + (", ".join("%s %s%s" % (t["lang"] or "untagged", t["fam"],
                     "" if t["main"] else " (extra)") for t in tracks) or "none"))
        old = self.fam.get(self.cur)
        changed = [(t["pid"], t["fam"], t["lang"], t["main"]) for t in tracks] != \
            [(t["pid"], t["fam"], t["lang"], t["main"]) for t in self.tracks]
        self.tracks, self.fam = tracks, fam
        if self.cur is not None and fam.get(self.cur) != old:
            self._drop()                # its codec changed: cut the new one from scratch
        es = bytes(keep) + b"".join(bytes([STYPE[s["fam"]], 0xE0 | s["pid"] >> 8, s["pid"] & 0xFF, 0xF0, 0])
                                    for s in SLOTS)
        body = bytes(sec[3:12 + pil]) + es
        n = len(body) + 4
        new = bytes([2, 0xB0 | n >> 8, n & 0xFF]) + body
        new += _crc(new).to_bytes(4, "big")
        self.pmt_out = self._packets(self.pmt_pid, b"\x00" + new)
        if changed or (self.cur is None and self.pend is None):   # the table repeats several times a second
            self._choose(first=True)

    # -- which track plays ------------------------------------------------------------------------------
    def now(self):
        """The audio clock now: the playing track's last frame moved on by the video time since (the provider's
        audio and video timestamps may sit apart), or the video clock before any audio."""
        if self.aclk is not None and self.va is not None and self.vclk is not None:
            return (self.aclk + dif(self.vclk, self.va)) % WRAP
        return self.vclk if self.aclk is None else self.aclk

    def _group(self, live):
        """The tracks that may play, best group first: the preferred language, else the main track's language
        (untagged tracks count as it), else everything; extra tracks (audio description...) only if nothing else."""
        main_lang = self.tracks[0]["lang"] if self.tracks else ""
        groups = []
        if self.pref:
            groups.append([t for t in live if t["lang"] == self.pref])
        groups.append([t for t in live if t["lang"] in ("", main_lang)])
        groups.append(live)
        for g in groups:
            if g:
                return [t for t in g if t["main"]] or g
        return []

    def _choose(self, first=False):
        clk = self.now()
        if first or clk is None or not self.seen:
            live = list(self.tracks)            # nothing heard yet: every listed track may deliver
        else:
            # Delivering: heard within the last second (audio is interleaved with the picture by timestamp), or not
            # heard yet while this stream's audio has only just started (tracks start a moment apart).
            early = self.t0 is not None and abs(dif(clk, self.t0)) < GAP
            live = [t for t in self.tracks if (t["pid"] in self.seen and abs(dif(clk, self.seen[t["pid"]])) < GAP)
                    or (t["pid"] not in self.seen and early)]
            if not live:
                return
        g = self._group(live)
        if not g:
            return
        best = max(g, key=lambda t: (self.ch.get(t["pid"], 0), t["fam"] in ("ac3", "eac3"), -t["order"]))
        cur = next((t for t in g if t["pid"] == self.cur), None)
        want = best["pid"]
        if cur is not None and want != cur["pid"]:
            # Same group and still playing: stay (a 5.1 track that is stereo during an ad is not traded away),
            # unless another track has carried real surround for a few checks in a row while this one is stereo.
            if self.ch.get(want, 0) >= 6 and self.ch.get(cur["pid"], 0) <= 2:
                self.up = [want, self.up[1] + 1 if self.up[0] == want else 1]
                if self.up[1] < 4:
                    want = cur["pid"]
            else:
                self.up = [None, 0]
                want = cur["pid"]
        else:
            self.up = [None, 0]
        if want != self.cur and want != self.pend:
            self.pend = want
            t = next(x for x in self.tracks if x["pid"] == want)
            self.log("audio: playing the %s %s track%s" % (t["lang"] or "untagged", t["fam"],
                     "" if self.cur is None else " (was another track)"))

    # -- the playing track, frame by frame ----------------------------------------------------------------
    def _flush(self):
        g, self.grp = self.grp, None
        if not g:
            return b""
        s, pts, data, dur = g
        out = self._fill(s, pts) + self._fill_all(pts, but=s) + self._pes(s, pts, b"".join(data))
        self.end[s] = (pts + dur) % WRAP
        self.aclk, self.va = pts, self.vclk
        return out

    def _cut(self):
        """Whole frames out of the playing track's buffer, each to the input of its format."""
        fam = self.fam.get(self.cur)
        out = []
        b = self.buf
        i = 0
        while i < len(b):
            f = aframes.frame(fam, b, i)
            if f is None:                       # not a frame start: find the next one
                i += 1
                continue
            size, key, samples, rate = f
            if not size:
                break                           # incomplete: wait for more
            pts = None
            while self.marks and self.marks[0][0] <= i:
                pts = self.marks.pop(0)[1]
            if pts is None:
                pts = self.npts
            if pts is None:                     # no timestamp yet: skip until a PES with one
                i += size
                continue
            if key is None:                     # E-AC-3 dependent substream: with its independent frame
                s = self.dep
                if s is None or not self.grp or self.grp[0] != s:
                    i += size
                    continue
            else:
                s = BYKEY.get(key)
                if s is None:
                    s = SPARE[fam]
                    if key not in self.said_keys:
                        self.said_keys.add(key)
                        self.log("audio: no input for the provider's format %s; using %s" % (key, SLOTS[s]["name"]))
                if fam == "eac3":
                    self.dep = s
            dur = samples * 90000 // rate if rate else 0
            e = (self.grp[1] + self.grp[3]) % WRAP if self.grp and self.grp[0] == s else self.end[s]
            c = self.now()
            if c is not None and abs(dif(pts, c)) > JUMP:   # far from the picture's clock: a stale or bad frame
                self.npts = None
                i += size
                continue
            if e is not None and key is not None:
                lag = dif(pts, e)
                if lag < -JUMP:                 # the provider's clock restarted: every input starts on the new one
                    out.append(self._flush())
                    self.end = [None] * len(SLOTS)
                elif lag < -9000:               # overlaps audio already sent (silence that ran ahead): skip it,
                    self.npts = (pts + dur) % WRAP   # never send a timestamp that goes back
                    i += size
                    continue
            if self.grp and (self.grp[0] != s or len(self.grp[2]) > 40 or
                             abs(dif(pts, (self.grp[1] + self.grp[3]) % WRAP)) > 900):
                out.append(self._flush())
            if self.grp is None:
                self.grp = [s, pts, [], 0]
            self.grp[2].append(bytes(b[i:i + size]))
            self.grp[3] += dur
            self.npts = (pts + dur) % WRAP
            i += size
        del b[:i]
        self.marks = [(o - i, t) for o, t in self.marks]
        if len(b) > MAXPES * 4:                # never a frame in sight: start over
            self._drop()
        return b"".join(out)

    # -- the stream -------------------------------------------------------------------------------------
    def _start(self, d):
        """A new encoder's first bytes: every input starts with one silent frame at the earliest timestamp of
        this chunk's audio and pictures, as early as the provider's own streams would have started it (ffmpeg
        times its output from the earliest stream), and early enough for ffmpeg's short probe to know every input.
        """
        base = None
        for i in range(0, len(d) - 187, 188):
            p = d[i:i + 188]
            if p[0] != 0x47 or not p[1] & 0x40:
                continue
            af = p[3] >> 4 & 3
            o = 5 + p[4] if af == 3 else 4
            if af == 2 or o + 14 > 188 or p[o:o + 3] != b"\x00\x00\x01" or not p[o + 7] & 0x80:
                continue
            pid = (p[1] & 31) << 8 | p[2]
            if not (0xE0 <= p[o + 3] <= 0xEF or pid in self.fam or 0xC0 <= p[o + 3] <= 0xDF):
                continue
            q = p[o + 9:o + 14]
            t = ((q[0] >> 1) & 7) << 30 | q[1] << 22 | (q[2] >> 1) << 15 | q[3] << 7 | q[4] >> 1
            if base is None or dif(t, base) < 0:
                base = t
        if base is None:
            return b""
        self.pre = False
        out = []
        for x in range(len(SLOTS)):
            if self.end[x] is None:
                out.append(self._pes(x, base, SLOTS[x]["frame"]))
                self.end[x] = (base + SLOTS[x]["dur"]) % WRAP
        return b"".join(out)

    def feed(self, d):
        out = [self._start(d)] if self.pre else []
        for i in range(0, len(d) - 187, 188):
            p = d[i:i + 188]
            if p[0] != 0x47:
                continue
            pid = (p[1] & 31) << 8 | p[2]
            pusi = p[1] & 0x40
            af = p[3] >> 4 & 3
            o = 5 + p[4] if af == 3 else 4
            if af == 2 or o >= 188:
                if pid != self.pmt_pid and pid not in self.fam:
                    out.append(p)
                continue
            if pid == 0:
                if pusi:
                    q = o + 1 + p[o]
                    if q + 12 <= 188 and p[q] == 0:
                        for j in range(q + 8, min(q + 3 + ((p[q + 1] & 15) << 8 | p[q + 2]) - 4, 184), 4):
                            if p[j] << 8 | p[j + 1]:
                                self.pmt_pid = (p[j + 2] & 31) << 8 | p[j + 3]
                                break
                    self.pat = bytes(p)
                out.append(p)
            elif pid == self.pmt_pid:
                if pusi:
                    q = o + 1 + p[o]
                    self.sec = bytearray(p[q:188]) if q < 188 else None
                elif self.sec is not None:
                    self.sec += p[o:188]
                if self.sec is not None and len(self.sec) >= 3:
                    need = 3 + ((self.sec[1] & 15) << 8 | self.sec[2])
                    if need > 1024:
                        self.sec = None
                    elif len(self.sec) >= need:
                        sec, self.sec = bytes(self.sec[:need]), None
                        self._pmt(sec)
                        out.append(self.pmt_out)
            elif pid in self.fam:
                pts = None
                pay = p[o:]
                if pusi and len(pay) >= 9 and pay[:3] == b"\x00\x00\x01":
                    if pay[7] & 0x80 and len(pay) >= 14:
                        q = pay[9:14]
                        pts = ((q[0] >> 1) & 7) << 30 | q[1] << 22 | (q[2] >> 1) << 15 | q[3] << 7 | q[4] >> 1
                    pay = pay[9 + pay[8]:]
                    if pts is not None:
                        self.seen[pid] = pts
                        if self.t0 is None:
                            self.t0 = pts
                        c = aframes.frame(self.fam[pid], pay, 0) if pay else None
                        if c and c[1] is not None:
                            self.ch[pid] = chans(c[1])
                        if self.said is None or abs(dif(pts, self.said)) > 45000:   # look again every half second
                            self.said = pts
                            self._choose()
                    if pid == self.pend and (pts is None or self.now() is None or abs(dif(pts, self.now())) < GAP):
                        self.cur, self.pend = pid, None
                        out.append(self._flush())
                        self._drop()
                if pid == self.cur:
                    if pts is not None:
                        if self.npts is not None and abs(dif(pts, self.npts)) > 45000:
                            self.buf.clear()    # audio was missing: what was left over cannot complete a frame
                            self.marks = []
                        self.marks.append((len(self.buf), pts))
                    self.buf += pay
                    out.append(self._cut())
            else:
                if pusi and o + 14 <= 188 and p[o:o + 3] == b"\x00\x00\x01" and 0xE0 <= p[o + 3] <= 0xEF \
                        and p[o + 7] & 0x80:
                    q = p[o + 9:o + 14]
                    v = ((q[0] >> 1) & 7) << 30 | q[1] << 22 | (q[2] >> 1) << 15 | q[3] << 7 | q[4] >> 1
                    self.vclk = v
                    if self.aclk is None or self.va is None or dif(v, self.va) > GAP:
                        # No audio playing (none on this stream, or the track stopped): silence on every input,
                        # moving with the picture, so the mix and the picture never wait for it.
                        out.append(self._fill_all((self.now() - LAG) % WRAP))
                        if self.aclk is not None and (self.said is None or abs(dif(v, self.said)) > 45000):
                            self.said = v
                            self._choose()
                out.append(p)
        out.append(self._flush())   # this chunk's audio goes out with it
        return b"".join(out)
