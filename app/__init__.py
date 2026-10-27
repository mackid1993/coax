"""coax: a small, frozen IPTV appliance."""

# Every request to a provider (playlists, player_api, XMLTV, streams) uses this
# user agent. It is fixed on purpose: IPTV panels accept it and it never needs tuning.
USER_AGENT = "TiviMate/5.1.6 (Android 12)"
