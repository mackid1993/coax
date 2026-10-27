# Vendored assets

Everything the web UI loads is committed here. No CDN, no npm, no build step. No JavaScript library is vendored:
the UI is plain ES modules (`app.js`, `api.js`, `ui.js`, `state.js`, `pages/*.js`).

## Fonts (SIL Open Font License 1.1)

| File | Source | Version | SHA-256 |
|---|---|---|---|
| `fonts/InterVariable.woff2` | `web/InterVariable.woff2` from https://github.com/rsms/inter/releases/download/v4.1/Inter-4.1.zip | Inter 4.1 | `693b77d4f32ee9b8bfc995589b5fad5e99adf2832738661f5402f9978429a8e3` |
| `fonts/OFL-Inter.txt` | `LICENSE.txt` from the same zip | | `262481e844521b326f5ecd053e59b98c8b2da78c8ee1bdbb6e8174305e54935a` |
| `fonts/JetBrainsMono-Regular.woff2` | `fonts/webfonts/` from https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip | JetBrains Mono 2.304 | `a9cb1cd82332b23a47e3a1239d25d13c86d16c4220695e34b243effa999f45f2` |
| `fonts/JetBrainsMono-Medium.woff2` | same zip | JetBrains Mono 2.304 | `086c48dfbea9ddaff1320f7e09399b8e2924e88ce67453721255db3bdbb5a353` |
| `fonts/OFL-JetBrainsMono.txt` | `OFL.txt` from the same zip | | `30f0c136e3c88e422d0791acd97238870f9054a9729bc34cf2ff0d4ed8cac4ad` |

Release archives as downloaded:
- `Inter-4.1.zip`: `9883fdd4a49d4fb66bd8177ba6625ef9a64aa45899767dde3d36aa425756b11e`
- `JetBrainsMono-2.304.zip`: `6f6376c6ed2960ea8a963cd7387ec9d76e3f629125bc33d1fdcd7eb7012f7bbf`

Verify with `shasum -a 256 app/web/vendor/fonts/*`.

## Brand

`../brand/*.svg` are copies of the official coax marks in `assets/brand/` (icon, favicon, dark and light
wordmarks).
