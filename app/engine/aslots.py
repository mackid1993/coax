"""Audio inputs: the provider's audio fed to the transcoder so that ffmpeg never sees an audio format change.

ffmpeg opens its audio decoders once, when it starts, and rebuilds its audio filters whenever the decoded format
changes. A provider switching codec mid-stream (an AAC ad in a Dolby show) leaves the decoder unable to read the
audio (silence), and a switch between 5.1 and stereo (every ad break) makes ffmpeg rebuild its filters, which
can drop a fraction of a second of sound when several inputs are mixed. So ffmpeg never sees either:

- The program table (PMT) lists fixed audio inputs, one per exact format (Dolby Digital 5.1 at 48 kHz, AAC stereo
  at 44.1 kHz, ...; SLOTS) on fixed PIDs, in place of the provider's audio tracks. ffmpeg decodes each and mixes
  them (amix, normalize=0) into the one output format. Each encoder opens the common formats (CORE) plus any
  other format its stream has shown (plan()), so a typical channel costs 7 inputs, not all of them.
- A format with no open input (one the stream had not shown when its encoder started, or one no input matches)
  is decoded by a small separate ffmpeg (Helper) to 5.1 PCM and fed to the PCM input, so it plays too, with no
  restart and no filter rebuild in the transcoder.
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
STYPE = {"ac3": 0x81, "eac3": 0x87, "aac": 0x0F, "latm": 0x11, "mp2": 0x03, "dts": 0x82, "pcm": 0x06}
SIDS = {"ac3": 0xBD, "eac3": 0xBD, "dts": 0xBD, "aac": 0xC0, "latm": 0xC0, "mp2": 0xC0, "pcm": 0xBD}
DESC = {"pcm": b"\x05\x04BSSD"}   # SMPTE 302M PCM is private data with a BSSD registration
WRAP = 1 << 33
LAG = 45000            # silence on the video clock trails the picture by 0.5 s
GAP = 90000            # no frame from the playing track for 1 s of video: every input gets silence
JUMP = 90000 * 10      # a clock jump (provider restart, wrap): an input's silence restarts there
MAXPES = 60000
HELPER_WAIT = 2.0      # a helper that was given audio but sent nothing back for 2 s is given up (silence instead)


def _silent_302m():
    """One silent SMPTE 302M frame: 1024 samples of 16-bit 5.1 at 48 kHz (the 'F' flag marks each 192-sample
    AES3 block start), as ffmpeg's s302m encoder writes it."""
    body = bytearray(15360)
    for k in range(0, 15360, 2880):
        body[k + 2] = body[k + 7] = body[k + 12] = 16
    return bytes([0x3C, 0x00, 0x80, 0x00]) + bytes(body)


SLOTS = []             # [{fam, name, pid, frame, key, dur}]
for _i, (_fam, _name, _b64) in enumerate(_TABLE):
    _f = base64.b64decode(_b64)
    _size, _key, _samples, _rate = aframes.frame(_fam, _f, 0)
    SLOTS.append({"fam": _fam, "name": _name, "pid": 0x1FC0 + _i, "frame": _f, "key": _key,
                  "dur": _samples * 90000 / _rate})       # exact: 44.1 kHz frames are not whole 90 kHz ticks
PCM = len(SLOTS)
SLOTS.append({"fam": "pcm", "name": "PCM 5.1 (other formats)", "pid": 0x1FC0 + PCM, "frame": _silent_302m(),
              "key": ("pcm",), "dur": 1920.0})
PIDS = tuple(s["pid"] for s in SLOTS)
BYKEY = {s["key"]: i for i, s in enumerate(SLOTS)}
# Always open: Dolby Digital 2.0 and 5.1, Dolby Digital Plus 2.0 and 5.1, AAC stereo at 48 and 44.1 kHz, HE-AAC,
# and PCM. A Dolby show with an AAC ad (or the other way round) plays through these with no change.
PCM_FALLBACK = False   # the PCM decoder (Helper) is not ready: in offline tests its PCM reached the mix late.
                       # Until it is, a format with no open input goes to its family's first input (one filter
                       # rebuild there, about 0.13 s of sound).
CORE = tuple(sorted(BYKEY[k] for k in (("ac3", 2, 0, 0), ("ac3", 7, 1, 0), ("eac3", 2, 0, 48000),
                                       ("eac3", 7, 1, 48000), ("aac", 1, 3, 2), ("aac", 1, 4, 2),
                                       ("aac", 1, 6, 2)))) + ((PCM,) if PCM_FALLBACK else ())
FIRST = {}             # a family's first input, opened when a stream lists that family
for _i, _s in enumerate(SLOTS):
    FIRST.setdefault(_s["fam"], _i)
HELPER_FMT = {"ac3": "ac3", "eac3": "eac3", "aac": "aac", "latm": "loas", "mp2": "mp3", "dts": "dts"}



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
# Codes providers use for the same spoken language, including common non-standard ones (esp for Spanish...).
SAME = {"fil": "tgl", "nob": "nor", "nno": "nor", "esp": "spa", "jap": "jpn", "chn": "zho", "sve": "swe", "arb": "ara"}


def canon(code):
    """A language tag as its ISO 639-2 code ("spa" for "es", "fra" for "fre"), or "" when it names no language."""
    c = (code or "").strip().lower()
    c = SAME.get(c, c)
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




def sync(d):
    """Where 188-byte packets start in d (three sync bytes in a row), or None."""
    for k in range(min(len(d), 188 * 64)):
        if d[k] == 0x47 and (k + 188 >= len(d) or d[k + 188] == 0x47) and (k + 376 >= len(d) or d[k + 376] == 0x47):
            if k + 188 < len(d):
                return k
    return None


def is_ts(d):
    """True for MPEG-TS (an ID3 tag in front allowed), False for something else (fMP4), None when d is too short."""
    if d[:3] == b"ID3" and len(d) >= 10:
        k = 10 + ((d[6] & 127) << 21 | (d[7] & 127) << 14 | (d[8] & 127) << 7 | (d[9] & 127))
        d = d[k:]
    if len(d) < 188 * 3:
        return None
    return sync(d[:188 * 4]) is not None


def _pes_head(sid, pts, n):
    p = pts % WRAP
    return bytes([0, 0, 1, sid, n >> 8 if n < 65536 else 0, n & 0xFF if n < 65536 else 0, 0x80, 0x80, 5,
                  0x21 | (p >> 29) & 0x0E, (p >> 22) & 0xFF, 0x01 | (p >> 14) & 0xFE, (p >> 7) & 0xFF,
                  0x01 | (p << 1) & 0xFE])


class Helper:
    """A small ffmpeg that decodes one format with no input of its own to 5.1 PCM (SMPTE 302M) for the PCM input.
    Frames go in as an elementary stream; PCM comes back with the helper's own timestamps, which are mapped back
    onto the provider's (base: the first frame's PTS). Reading runs on its own thread."""

    def __init__(self, ff, fam, base, log):
        import subprocess
        import threading
        import collections
        self.fam, self.base, self.log = fam, base, log
        self.next = base                 # the provider PTS the next frame fed should have
        self.out = collections.deque()   # (helper PTS, 302M frame)
        self.h0 = None                   # the helper's first PTS
        self.done = False
        self.fed = 0
        self.got = 0                     # 302M frames (1024 samples) decoded so far
        import time
        self.mono = time.monotonic
        self.t_in = self.t_out = self.mono()
        try:
            self.p = subprocess.Popen(
                [ff, "-hide_banner", "-loglevel", "error", "-probesize", "32768", "-analyzeduration", "0",
                 "-fflags", "nobuffer", "-f", HELPER_FMT[fam], "-i", "pipe:0",
                 "-af", "aresample=48000,aformat=sample_rates=48000:channel_layouts=5.1",
                 "-strict", "-2", "-c:a", "s302m", "-muxdelay", "0", "-muxpreload", "0", "-flush_packets", "1",
                 "-f", "mpegts", "pipe:1"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
        except Exception as exc:
            log("audio: could not start the decoder for %s (%s)" % (fam, exc))
            self.p, self.done = None, True
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        buf, cur = b"", None
        try:
            while True:
                data = self.p.stdout.read(188 * 64)
                if not data:
                    break
                buf += data
                n = len(buf) // 188 * 188
                for i in range(0, n, 188):
                    p = buf[i:i + 188]
                    if p[0] != 0x47 or ((p[1] & 31) << 8 | p[2]) != 0x100:
                        continue
                    af = p[3] >> 4 & 3
                    if af == 2:
                        continue
                    o = 5 + p[4] if af == 3 else 4
                    if p[1] & 0x40:
                        if cur:
                            self.out.append(cur)
                            self.t_out = self.mono()
                            self.got += 1
                        q = p[o + 9:o + 14]
                        t = ((q[0] >> 1) & 7) << 30 | q[1] << 22 | (q[2] >> 1) << 15 | q[3] << 7 | q[4] >> 1
                        cur = (t, bytearray(p[o + 9 + p[o + 8]:]))
                    elif cur:
                        cur[1].extend(p[o:])
                buf = buf[n:]
            if cur:
                self.out.append(cur)
        except Exception:
            pass
        self.done = True

    def write(self, frame, pts):
        if self.p is None:
            return False
        if abs(dif(pts, self.next)) > 9000:
            return False                 # not continuous with what it decoded: the caller starts a new one
        try:
            self.p.stdin.write(frame)
            self.p.stdin.flush()
        except Exception:
            self.done = True
            return False
        self.fed += 1
        if self.t_in <= self.t_out:
            self.t_in = self.mono()      # waiting for output since this
        return True

    def wait(self):
        """Hold the writer (a few milliseconds; at most 0.3 s) until the helper has decoded about all it was
        given, so its PCM goes into the stream where it belongs and the mix never waits on it."""
        if self.p is None or self.done:
            return
        t = self.mono()
        while self.got * 1920 + 13500 < getattr(self, "nf", 0.0) and self.mono() - t < 0.3 and not self.done:
            import time
            time.sleep(0.002)

    def stuck(self):
        return self.t_in > self.t_out and self.mono() - self.t_out > HELPER_WAIT and not self.out

    def advance(self, dur):
        self.nf = getattr(self, "nf", 0.0) + dur
        self.next = (self.base + int(round(self.nf))) % WRAP

    def close(self):
        try:
            if self.p:
                self.p.stdin.close()
        except Exception:
            pass

    def kill(self):
        self.close()
        try:
            if self.p:
                self.p.kill()
        except Exception:
            pass

    def frames(self):
        """(provider PTS, 302M frame) decoded so far."""
        res = []
        while self.out:
            t, f = self.out.popleft()
            if self.h0 is None:
                self.h0 = t
            res.append(((self.base + dif(t, self.h0)) % WRAP, bytes(f)))
        return res


class Feeder:
    """Rewrites one engine's transcoder input; see the module docstring. pref: preferred language ("" for the
    provider's main track). ff: the ffmpeg to run helpers with. Not thread safe: call it from the writer only."""

    def __init__(self, pref="", log=None, ff="ffmpeg"):
        self.pref = canon(pref)
        self.log = log or (lambda m: None)
        self.ff = ff
        self.pat = b""
        self.pmt_pid = None
        self.sec = None             # PMT section being assembled
        self.last_sec = None        # the provider's last PMT section, to rebuild ours when the inputs change
        self.pmt_out = b""          # our PMT, packetized
        self.pmt_cc = 0
        self.tracks = []            # [{pid, fam, lang, main, order}] in the provider's order
        self.fam = {}               # provider audio pid -> family
        self.keys = {}              # provider audio pid -> the formats its frames have shown
        self.cc = [0] * len(SLOTS)
        self.active = CORE          # the inputs the running encoder has
        self.helper = None
        self.said_keys = set()
        self.rest = b""             # a packet split across two chunks
        self.bypass = False         # not MPEG-TS (fMP4 HLS): passed through, the encoder maps the audio itself
        self.built_bypass = None    # what the running encoder was built for
        self.reset()

    def reset(self):
        """A new stream (failover, hand switch): its clock and tracks start over."""
        self.end = [None] * len(SLOTS)
        self.frac = [0.0] * len(SLOTS)  # fraction of a 90 kHz tick each input's silence is owed
        self.seen, self.ch = {}, {}
        self.vclk = self.aclk = None
        self.vadv = 0               # picture time since the playing track's last frame (gaps and jumps not counted)
        self.vmax = None            # the newest picture timestamp seen (pictures arrive out of order with B-frames)
        self.rest = b""
        self.cur = self.pend = None
        self.up = [None, 0]
        self.said = None
        self.t0 = None              # the first audio PTS heard on this stream
        self.pre = True             # a new encoder: its inputs start where its first audio or picture does
        self.tracks, self.fam, self.keys = [], {}, {}   # a new stream lists its own tracks (its table is read
        self.sec = None                                  # again from its first bytes, before its encoder starts)
        self.missing = None         # a format family with no open input appeared: the engine restarts at a keyframe
        self._end_helper()
        self._drop()

    def _drop(self):
        self.buf = bytearray()      # the playing track's bytes not yet cut into frames
        self.marks = []             # [(offset in buf, PTS)] where its PES packets began
        self.npts = None            # the next frame's PTS when its PES carried none
        self.grp = None             # [input, first PTS, [frames], duration] waiting to be sent

    def _end_helper(self, now=False):
        if self.helper:
            if now:
                self.helper.kill()
                self.helper = None
            else:
                self.helper.close()  # let it finish what it was given; drained in _helper_out()

    # -- which inputs an encoder opens --------------------------------------------------------------------
    def plan(self):
        """The inputs for the next encoder: the common ones, plus one for every other format this stream has
        listed or shown. Called when an encoder is built; the PMT follows it."""
        want = set(CORE)
        for t in self.tracks:
            if t["fam"] in ("mp2", "latm", "dts"):
                want.add(FIRST[t["fam"]])
        for ks in self.keys.values():
            for k in ks:
                if k in BYKEY:
                    want.add(BYKEY[k])
        self.active = tuple(sorted(want))
        self.built_bypass = self.bypass
        self.missing = None
        if self.last_sec:
            self._build()
        return None if self.bypass else [SLOTS[x] for x in self.active]

    def wants_more(self, d):
        """Whether the bytes a new encoder is about to get need a different encoder: a format it has no input
        for, or not MPEG-TS at all (fMP4 HLS) when it was built for TS, or the other way round. It can then be
        replaced before it has had anything: nobody sees that."""
        ts = is_ts(d)
        if ts is not None and (not ts) != self.bypass:
            self.bypass = not ts
            self.log("audio: the provider sends %s" % ("MPEG-TS" if ts else "a format other than MPEG-TS; its "
                                                        "audio goes to the encoder as it is"))
        if self.bypass:
            return self.built_bypass is not True
        if self.built_bypass:
            return True
        self._learn(d)
        want = set(self.active)
        for t in self.tracks:
            if t["fam"] in ("mp2", "latm", "dts"):
                want.add(FIRST[t["fam"]])
        for ks in self.keys.values():
            want.update(BYKEY[k] for k in ks if k in BYKEY)
        return not want <= set(self.active)

    def learn(self, d):
        """Read tracks and formats from bytes about to go to a new encoder (the whole segment, keyframe or not)."""
        if not self.bypass:
            self._learn(d)

    def _learn(self, d):
        """Tracks and formats from raw bytes, without sending anything (for wants_more)."""
        for i in range(0, len(d) - 187, 188):
            p = d[i:i + 188]
            if p[0] != 0x47 or not p[1] & 0x40:
                continue
            pid = (p[1] & 31) << 8 | p[2]
            af = p[3] >> 4 & 3
            if af == 2:
                continue
            o = 5 + p[4] if af == 3 else 4
            if o >= 188:
                continue
            if pid == 0:
                q = o + 1 + p[o]
                if q + 16 <= 188 and p[q] == 0:
                    for j in range(q + 8, min(q + 3 + ((p[q + 1] & 15) << 8 | p[q + 2]) - 4, 184), 4):
                        if p[j] << 8 | p[j + 1]:
                            self.pmt_pid = (p[j + 2] & 31) << 8 | p[j + 3]
                            break
            elif pid == self.pmt_pid:
                q = o + 1 + p[o]
                if q + 3 <= 188 and p[q] == 2:
                    need = 3 + ((p[q + 1] & 15) << 8 | p[q + 2])
                    if q + need <= 188:
                        self._pmt(bytes(p[q:q + need]), send=False)
            elif pid in self.fam:
                pay = p[o:]
                if pay[:3] == b"\x00\x00\x01" and len(pay) > 9:
                    f = aframes.frame(self.fam[pid], pay[9 + pay[8]:], 0)
                    if f and f[1] is not None:
                        self.keys.setdefault(pid, set()).add(f[1])

    def psi(self, fresh=True):
        """For the first bytes a new encoder gets: PAT and our PMT (it may start mid-segment, before the next
        table), so ffmpeg knows every input at once. The inputs' silence starts again from the audio that
        follows: what went to the old encoder must not put the new one's audio ahead of its first picture.
        fresh=False (the same bytes written again to a replacement encoder): only the tables."""
        if self.bypass:
            return b""
        if not fresh:
            return self.pat + self.pmt_out
        self.end = [None] * len(SLOTS)
        self.frac = [0.0] * len(SLOTS)
        self.grp = None
        self.aclk = None
        self.vadv = 0
        self.pre = True
        self._end_helper(now=True)
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
        return self._packets(SLOTS[s]["pid"], _pes_head(SIDS[SLOTS[s]["fam"]], pts, 8 + len(data)) + data, s)

    def _fill(self, s, upto):
        """Silent frames in input s up to upto."""
        e = self.end[s]
        if e is None or abs(dif(upto, e)) > JUMP:
            self.end[s] = upto % WRAP
            return b""
        d = SLOTS[s]["dur"]
        n = int((dif(upto, e) - self.frac[s]) // d)
        out = []
        while n > 0:
            k = min(n, 30 if s != PCM else 1)   # 302M: one frame per PES (its header gives the PES size)
            out.append(self._pes(s, e, SLOTS[s]["frame"] * k))
            t = self.frac[s] + k * d
            e += int(t)
            self.frac[s] = t - int(t)
            n -= k
        self.end[s] = e % WRAP
        return b"".join(out)

    def _fill_all(self, upto, but=None):
        return b"".join(self._fill(x, upto) for x in self.active
                        if x != but and not (x == PCM and self.helper))

    # -- program table ----------------------------------------------------------------------------------
    def _pmt(self, sec, send=True):
        """Our PMT from the provider's: its video and other tracks, its audio replaced by the open inputs."""
        if len(sec) < 16 or sec[0] != 2:
            return
        pil = (sec[10] & 15) << 8 | sec[11]
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
            j += 5 + il
        changed = [(t["pid"], t["fam"], t["lang"], t["main"]) for t in tracks] != \
            [(t["pid"], t["fam"], t["lang"], t["main"]) for t in self.tracks]
        if changed and send:
            self.log("audio tracks: " + (", ".join("%s %s%s" % (t["lang"] or "untagged", t["fam"],
                     "" if t["main"] else " (extra)") for t in tracks) or "none"))
        old = self.fam.get(self.cur)
        self.tracks, self.fam, self.last_sec = tracks, fam, bytes(sec)
        for pid in list(self.keys):
            if pid not in fam:
                del self.keys[pid]
        if self.cur is not None and fam.get(self.cur) != old:
            self._drop()                # its codec changed: cut the new one from scratch
        self._build()
        if send and (changed or (self.cur is None and self.pend is None)):   # the table repeats every moment
            self._choose(first=True)

    def _build(self):
        sec = self.last_sec
        pil = (sec[10] & 15) << 8 | sec[11]
        keep = bytearray()
        j = 12 + pil
        while j + 5 <= len(sec) - 4:
            st, pid, il = sec[j], (sec[j + 1] & 31) << 8 | sec[j + 2], (sec[j + 3] & 15) << 8 | sec[j + 4]
            if family(st, bytes(sec[j + 5:j + 5 + il])) is None and pid not in PIDS:
                keep += sec[j:j + 5 + il]   # video, subtitles, data and audio coax cannot take stay as they are
            j += 5 + il
        es = bytes(keep)
        for x in self.active:
            s = SLOTS[x]
            d = DESC.get(s["fam"], b"")
            es += bytes([STYPE[s["fam"]], 0xE0 | s["pid"] >> 8, s["pid"] & 0xFF, 0xF0, len(d)]) + d
        body = bytes(sec[3:12 + pil]) + es
        n = len(body) + 4
        new = bytes([2, 0xB0 | n >> 8, n & 0xFF]) + body
        new += _crc(new).to_bytes(4, "big")
        if self.pmt_pid is not None:
            self.pmt_out = self._packets(self.pmt_pid, b"\x00" + new)

    # -- which track plays ------------------------------------------------------------------------------
    def now(self):
        """The audio clock now: the playing track's last frame moved on by the video time since (the provider's
        audio and video timestamps may sit apart), or the video clock before any audio."""
        if self.aclk is not None:
            return (self.aclk + self.vadv) % WRAP
        return self.vclk

    def _group(self, live):
        """The tracks that may play, best group first: the preferred language, else the main track's language
        (untagged tracks count as it), else everything; extra tracks (audio description...) only if nothing else."""
        mains = [t for t in self.tracks if t["main"]]
        main_lang = (mains or self.tracks or [{"lang": ""}])[0]["lang"]
        if any(t["main"] for t in live):
            live = [t for t in live if t["main"]]   # an extra track never wins while a main one plays
        groups = []
        if self.pref:
            groups.append([t for t in live if t["lang"] == self.pref])
        groups.append([t for t in live if t["lang"] in ("", main_lang)])
        groups.append(live)
        for g in groups:
            if g:
                return g
        return []

    def _aclock(self):
        """The audio clock for liveness: the playing track's, or before any track plays the newest audio heard
        (the provider's audio timestamps may sit seconds away from its pictures')."""
        if self.aclk is not None:
            return self.now()
        best = None
        for t in self.seen.values():
            if best is None or dif(t, best) > 0:
                best = t
        return best

    def _choose(self, first=False):
        clk = self._aclock()
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
        self.end[s] = (pts + int(dur)) % WRAP
        self.frac[s] = dur - int(dur)
        return out

    def _to_helper(self, fam, frame, pts, dur):
        """A frame of a format with no open input: decode it to PCM in a helper."""
        if self.helper and (self.helper.fam != fam or self.helper.done or not self.helper.write(frame, pts)):
            self._end_helper()
            self.helper = None
        if self.helper is None:
            self.helper = Helper(self.ff, fam, pts, self.log)
            if not self.helper.write(frame, pts):
                self.helper = None
                return
        self.helper.advance(dur)
        self.aclk, self.vadv = pts, 0

    def _helper_out(self):
        """PCM the helper has decoded, on the PCM input; a helper that fell far behind or ended is dropped."""
        h = self.helper
        if not h:
            return b""
        h.wait()
        out = []
        for t, f in h.frames():
            e = self.end[PCM]
            if e is not None and dif(t, e) < -9000:
                continue                         # never back in time
            out.append(self._fill(PCM, t))
            out.append(self._pes(PCM, t, f))
            n = (f[0] << 8 | f[1]) // (((f[2] >> 6) + 1) * 5) if len(f) > 4 else 1024
            self.end[PCM] = (t + n * 90000 // 48000) % WRAP
        dead = h.stuck()
        if h.done and not h.out or dead:
            if dead and not h.done:
                self.log("audio: the decoder for %s stopped answering; filling with silence" % h.fam)
            h.kill()
            self.helper = None
        return b"".join(out)

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
            if key is None:                     # an E-AC-3 dependent substream with no frame before it here
                if self.helper and self.helper.fam == fam:
                    self.helper.write(bytes(b[i:i + size]), self.helper.next)
                i += size
                continue
            size_all = size
            if fam == "eac3":
                # An independent frame followed by dependent substreams (7.1 and the like) decodes to a layout no
                # input has: the whole set goes to the PCM decoder.
                k = i + size
                while True:
                    g = aframes.frame(fam, b, k)
                    if g and not g[0]:
                        break                   # the next frame is not complete yet
                    if not g or g[1] is not None:
                        break
                    k += g[0]
                if g and not g[0]:
                    break                       # wait until the dependent part (if any) is here
                size_all = k - i
                if size_all > size:
                    key = key + ("dependent",)
            pts = None
            while self.marks and self.marks[0][0] <= i:
                pts = self.marks.pop(0)[1]
            if pts is None and self.npts is not None:
                pts = int(round(self.npts)) % WRAP
            if pts is None:                     # no timestamp yet: skip until a PES with one
                i += size_all
                continue
            dur = samples * 90000 / rate if rate else 0.0
            data = bytes(b[i:i + size_all])
            self.keys.setdefault(self.cur, set()).add(key)
            s = BYKEY.get(key)
            if s is None or s not in self.active:
                fb = PCM if PCM_FALLBACK else None
                if fb is None:   # the open input of the same family closest in channels (5.1 to 5.1, 2.0 to 2.0)
                    same = [x for x in self.active if SLOTS[x]["fam"] == fam]
                    fb = min(same, key=lambda x: abs(chans(SLOTS[x]["key"]) - chans(key))) if same else None
                if fb not in self.active:
                    # No input of this family at all (a codec this encoder was not built for): never give it to
                    # another family's decoder; drop it and ask for an encoder with the input, at a keyframe.
                    if self.missing is None:
                        self.missing = fam
                        self.log("audio: the provider switched to %s, which this encoder has no input for" % fam)
                    i += size_all
                    continue
                if key not in self.said_keys:
                    self.said_keys.add(key)
                    self.log("audio: the provider's format %s has no open input; using %s" % (key, SLOTS[fb]["name"]))
                s = fb
            c = self.now()
            if c is not None and abs(dif(pts, c)) > JUMP:
                if self.vclk is not None and abs(dif(pts, self.vclk)) < JUMP:
                    # The provider's clock moved (a jump in a running stream) and the pictures moved with it: the
                    # audio follows, and every input starts again on the new clock.
                    out.append(self._flush())
                    self.end = [None] * len(SLOTS)
                    self.aclk, self.vadv = pts, 0
                else:                           # far from both clocks: a stale or bad frame
                    self.npts = None
                    i += size_all
                    continue
            self.npts = pts + dur
            self.aclk, self.vadv = pts, 0
            if s == PCM:
                if self.grp:
                    out.append(self._flush())
                self._to_helper(fam, data, pts, dur)
                i += size_all
                continue
            if self.helper:
                self._end_helper()          # back to an open input: the helper finishes what it has
            e = (self.grp[1] + int(self.grp[3])) % WRAP if self.grp and self.grp[0] == s else self.end[s]
            if e is not None:
                lag = dif(pts, e)
                if lag < -JUMP:                 # the provider's clock restarted: every input starts on the new one
                    out.append(self._flush())
                    self.end = [None] * len(SLOTS)
                elif lag < -6000:               # overlaps audio already sent (silence that ran ahead): skip it,
                    i += size_all               # never send a timestamp that goes back
                    continue
            if self.grp and (self.grp[0] != s or len(self.grp[2]) > 40 or
                             abs(dif(pts, (self.grp[1] + int(round(self.grp[3]))) % WRAP)) > 900):
                out.append(self._flush())
            if self.grp is None:
                self.grp = [s, pts, [], 0.0]
            self.grp[2].append(data)
            self.grp[3] += dur
            i += size_all
        del b[:i]
        self.marks = [(o - i, t) for o, t in self.marks]
        if len(b) > MAXPES * 4:                # never a frame in sight: start over
            self._drop()
        return b"".join(out)

    # -- the stream -------------------------------------------------------------------------------------
    def _start(self, d):
        """A new encoder's first bytes: every open input starts with one silent frame at the earliest timestamp
        of this chunk's audio and pictures, as early as the provider's own streams would have started it (ffmpeg
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
        for x in self.active:
            if self.end[x] is None:
                out.append(self._pes(x, base, SLOTS[x]["frame"]))
                d = SLOTS[x]["dur"]
                self.end[x] = (base + int(d)) % WRAP
                self.frac[x] = d - int(d)
        return b"".join(out)

    def feed(self, d):
        if self.bypass:
            return d
        d = self.rest + d
        k = 0
        if d[:3] == b"ID3" and len(d) >= 10:   # an ID3 tag in front of a segment's packets, any length
            n = 10 + ((d[6] & 127) << 21 | (d[7] & 127) << 14 | (d[8] & 127) << 7 | (d[9] & 127))
            if len(d) < n + 188:
                self.rest = d
                return b""
            d = d[n:]
        if not (len(d) >= 188 and d[0] == 0x47 and (len(d) < 376 or d[188] == 0x47)):
            k = sync(d)                 # an ID3 tag before the packets, or a chunk that split a packet
            if k is None:
                self.rest = d[-187:] if len(d) > 187 else d
                return b""
        n = k + (len(d) - k) // 188 * 188
        self.rest = d[n:]
        d = d[k:n]
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
                        out.append(self._flush())   # what was cut before the table goes out before any change
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
                            self.keys.setdefault(pid, set()).add(c[1])
                        if self.said is None or abs(dif(pts, self.said)) > 45000:   # look again every half second
                            self.said = pts
                            self._choose()
                    c = self._aclock()
                    if pid == self.pend and (pts is None or c is None or abs(dif(pts, c)) < GAP):
                        self.cur, self.pend = pid, None
                        out.append(self._flush())
                        self._drop()
                if pid == self.cur:
                    if pts is not None:
                        if self.npts is not None and abs(dif(pts, int(self.npts) % WRAP)) > 45000:
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
                    # Picture time moves on only past the newest timestamp seen: B-frames arrive out of order, and
                    # counting every step forward would run this clock fast. A jump or a gap is not audio time.
                    step = dif(v, self.vmax) if self.vmax is not None else 0
                    if 0 < step < 27000:
                        self.vadv += step
                    if self.vmax is None or step > 0 or step < -JUMP:
                        self.vmax = v
                    self.vclk = v
                    if self.aclk is None or self.vadv > GAP:
                        # No audio playing (none on this stream, or the track stopped): silence on every input,
                        # moving with the picture, so the mix and the picture never wait for it.
                        out.append(self._flush())   # audio cut in this chunk goes out first, never behind silence
                        out.append(self._fill_all((self.now() - LAG) % WRAP))
                        if self.aclk is not None and (self.said is None or abs(dif(v, self.said)) > 45000):
                            self.said = v
                            self._choose()
                out.append(p)
        out.append(self._flush())   # this chunk's audio goes out with it
        out.append(self._helper_out())
        return b"".join(out)
