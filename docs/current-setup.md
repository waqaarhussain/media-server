# Captured code versus live configuration

Code baseline: Telegram-Bot-Update-Command.txt, version 11, built 2026-11-02. `bot/bot.py` and `bot/catalog.py` are extracted verbatim from that command. There are no embedded Telegram tokens or API keys.

Known Appbox paths and integration:

| Component | Current path/value |
| --- | --- |
| Service account | appbox |
| Radarr | `/opt/Radarr`, `/var/lib/radarr`, localhost:7878 |
| Prowlarr | `/opt/Prowlarr`, `/var/lib/prowlarr`, localhost:9696/prowlarr |
| Bot source | `~/.local/share/media-request-bot` |
| Bot state | `~/.local/state/media-request-bot` |
| Bot credentials | `~/.config/media-request-bot` |
| Movies | `/APPBOX_DATA/storage/Media/Movies` |
| Kids Movies | `/APPBOX_DATA/storage/Media/Kids/Movies` |
| Completed downloads | `/APPBOX_DATA/storage/Torrents/Completed/radarr` |
| Incomplete downloads | `/APPBOX_DATA/storage/Torrents/Current Downloads/radarr` |
| qBittorrent category | radarr |
| Profile | 4K Balanced |
| Known public indexer | Knaben, through Prowlarr |

These notes are not a substitute for exporting the live app settings. Exact current profiles, custom-format scores, indexer URLs, version numbers, Nginx configuration and credentials are only authoritative in the runtime snapshot made on the source VPS. No guesses are installed in their place.

Each movie stays inside its own movie folder for Radarr. Infuse's library view supplies the flat poster view without moving the files out of Radarr's folders.
