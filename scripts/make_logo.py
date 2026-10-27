"""Generate the coax logo set (icon, favicon, wordmarks) as clean SVG."""
import math
import os
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
os.makedirs(OUT, exist_ok=True)

DEFS = """<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#151C38"/><stop offset="1" stop-color="#070A16"/></linearGradient>
<linearGradient id="jacket" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5EEAD4"/><stop offset=".55" stop-color="#22D3EE"/><stop offset="1" stop-color="#6366F1"/></linearGradient>
<radialGradient id="copper" cx=".36" cy=".32" r=".78"><stop offset="0" stop-color="#FFE7BF"/><stop offset=".45" stop-color="#F59E0B"/><stop offset="1" stop-color="#B45309"/></radialGradient>
<radialGradient id="glow" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="#F59E0B" stop-opacity=".55"/><stop offset="1" stop-color="#F59E0B" stop-opacity="0"/></radialGradient>
<radialGradient id="diel" cx=".5" cy=".42" r=".6"><stop offset="0" stop-color="#E2E8F0" stop-opacity=".16"/><stop offset="1" stop-color="#E2E8F0" stop-opacity=".04"/></radialGradient>
<linearGradient id="sheen" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".10"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/></linearGradient>
</defs>"""


def pt(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def arc(cx, cy, r, a0, a1):
    x0, y0 = pt(cx, cy, r, a0)
    x1, y1 = pt(cx, cy, r, a1)
    large = 1 if (a1 - a0) % 360 > 180 else 0
    return f"M{x0:.2f} {y0:.2f}A{r:.2f} {r:.2f} 0 {large} 1 {x1:.2f} {y1:.2f}"


def mark(cx, cy, s, braid=True, braid_color="#CBD5E1"):
    """Cross-section of a coaxial cable: jacket, braided shield, dielectric, glowing copper core."""
    g = [f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{s:.2f}" fill="none" stroke="url(#jacket)" stroke-width="{s * .17:.2f}"/>']
    if braid:
        n, rb = 36, s * .74
        step = 2 * math.pi * rb / n
        g.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{rb:.2f}" fill="none" stroke="{braid_color}" stroke-opacity=".55" '
                 f'stroke-width="{s * .07:.2f}" stroke-dasharray="{step * .55:.2f} {step * .45:.2f}" stroke-linecap="round" '
                 f'transform="rotate(-90 {cx:.2f} {cy:.2f})"/>')
    g.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{s * .62:.2f}" fill="url(#diel)"/>')
    g.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{s * .52:.2f}" fill="url(#glow)"/>')
    g.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{s * .21:.2f}" fill="url(#copper)"/>')
    return "".join(g)


def icon(braid=True, size=512):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="{size}" height="{size}">{DEFS}'
            '<rect x="16" y="16" width="480" height="480" rx="112" fill="url(#bg)"/>'
            '<rect x="16" y="16" width="480" height="480" rx="112" fill="url(#sheen)"/>'
            '<rect x="17" y="17" width="478" height="478" rx="111" fill="none" stroke="#fff" stroke-opacity=".06" stroke-width="2"/>'
            f'{mark(256, 256, 150 if braid else 165, braid)}</svg>')


def wordmark(color, scale=2, braid_color="#CBD5E1"):
    """'coax' set in geometric strokes; the o is the cable cross-section."""
    sw, h = 26, 120
    r = (h - sw) / 2
    cy = h / 2 + 10
    gap = 20
    p = []
    x = sw / 2 + r
    p.append(f'<path d="{arc(x, cy, r, 42, 318)}" fill="none" stroke="{color}" stroke-width="{sw}" stroke-linecap="round"/>')
    left = x + r + sw / 2 - 9              # optical: the open c leaves extra air
    s = (h / 2) / 1.085                      # jacket outer edge matches the letters' x-height
    ox = left + gap + h / 2
    p.append(mark(ox, cy, s, True, braid_color))
    left = ox + h / 2
    ax = left + gap + sw / 2 + r
    p.append(f'<circle cx="{ax:.2f}" cy="{cy:.2f}" r="{r:.2f}" fill="none" stroke="{color}" stroke-width="{sw}"/>')
    p.append(f'<path d="M{ax + r:.2f} {cy - r:.2f}V{cy + r:.2f}" stroke="{color}" stroke-width="{sw}" stroke-linecap="round"/>')
    left = ax + r + sw / 2
    xs = left + gap + sw / 2
    xe = xs + r * 1.75
    p.append(f'<path d="M{xs:.2f} {cy - r:.2f}L{xe:.2f} {cy + r:.2f}M{xe:.2f} {cy - r:.2f}L{xs:.2f} {cy + r:.2f}" '
             f'stroke="{color}" stroke-width="{sw}" stroke-linecap="round"/>')
    w, hh = xe + sw / 2 + 4, h + 20
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="-4 0 {w + 8:.0f} {hh}" '
            f'width="{(w + 8) * scale:.0f}" height="{hh * scale}">{DEFS}{"".join(p)}</svg>')


files = {
    "coax-icon.svg": icon(True),
    "coax-favicon.svg": icon(False),
    "coax-wordmark-dark.svg": wordmark("#E8EEF9"),
    "coax-wordmark-light.svg": wordmark("#0F172A", 2, "#64748B"),
}
for name, svg in files.items():
    with open(os.path.join(OUT, name), "w") as f:
        f.write(svg + "\n")

preview = ('<html><body style="margin:0">'
           '<div style="display:flex;gap:40px;padding:36px;background:#0A0E1C;align-items:center">'
           f'{icon(True, 220)}<div style="display:flex;flex-direction:column;gap:26px">{wordmark("#E8EEF9", 1.6)}'
           f'<div style="display:flex;gap:18px;align-items:center">{icon(True, 64)}{icon(False, 32)}{icon(False, 16)}</div></div></div>'
           f'<div style="padding:30px 36px;background:#F6F7FB">{wordmark("#0F172A", 1.6, "#64748B")}</div></body></html>')
with open(os.path.join(OUT, "preview.html"), "w") as f:
    f.write(preview)
print("wrote", ", ".join(files))
