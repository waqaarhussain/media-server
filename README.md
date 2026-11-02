# Media Server

Waqaar's native Ubuntu media stack: Radarr, Prowlarr and a private Telegram movie bot. One command to adopt/install, `update-live` for bot updates, and encrypted backup/restore for an identical replacement VPS.

Built for **Ubuntu 24.04 or newer**, systemd and a native Radarr/Prowlarr installation. Existing Appbox media storage stays mounted separately. The bot streams nothing itself; Infuse reads the media through your existing file share.

## What it configures

| Component | Behaviour |
| --- | --- |
| Radarr | Installs native binaries on a blank server; adopts existing installation/settings unchanged |
| Prowlarr | Installs native binaries with `/prowlarr` URL base; preserves existing indexers and app links |
| Telegram bot | Deploys the current single-panel movie search, downloads, library, recommendations and confirmed deletion flow |
| Recommendations | Builds an IMDb catalogue and refreshes it daily around 04:00 UK time |
| Updates | `update-live` pulls this repository and updates the bot without upgrading the native apps |
| Backup | Passphrase-encrypted snapshot of the running setup, installed app bundles, credentials and app data; movie files excluded |
| Restore | Restores captured settings/versions to a fresh compatible Ubuntu VPS; leaves Telegram stopped until you start it |
| Folders | Prompts for Movies/Kids Movies roots when needed and provides a path migration helper |

An exact rebuild requires the snapshot from your current VPS. Fresh installers cannot infer private or subsequently changed settings.

## Current VPS: adopt it, then capture the real settings

This repository is public. No GitHub login or token is needed. Run this as `appbox` on the current VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/waqaarhussain/media-server/main/bootstrap.sh -o /tmp/media-server-bootstrap.sh && bash /tmp/media-server-bootstrap.sh
media-server backup
```

The installer adopts existing `/opt/Radarr`, `/opt/Prowlarr`, `/var/lib/radarr`, `/var/lib/prowlarr` and the saved Telegram configuration. It does not overwrite existing native application settings or service files. It deploys the latest bot from this repository. Root installation defaults to `appbox`; installation through sudo uses the caller. `--user appbox` sets it explicitly.

Backup asks for an encryption passphrase twice and prints one `.tar.gz.gpg` path under `~/media-server-backups`. Copy that file to your third-party storage. Keep the passphrase separately. The databases are copied while the relevant services are stopped briefly; those previously running are restarted, including if the copy fails. Encryption happens after services restart.

**The repository alone is not a backup of live settings.** Run `media-server backup` on the current VPS. Until that happens, no live snapshot has been captured. Run it again after changing profiles/indexers/client settings, or before migration.

## What the encrypted snapshot includes

- Radarr and Prowlarr databases, app configuration, authentication settings, API keys, quality profiles, custom formats, indexers, app sync, download-client connection credentials, library records, history and remote path mappings.
- Their actual installed application bundles, preserving the captured versions instead of depending on the latest upstream version.
- Telegram token, allowed user/chat IDs, Radarr connection and folders; installed bot code; watchlist, download tracking, preview ownership, recommendation index and pending state.
- Host reference copies of native service units/overrides, bot services/timer, Nginx configuration, Let's Encrypt configuration if present, fstab, helper commands, and user/root crontabs.
- A manifest with source CPU architecture, C library version, app versions, timestamp and repository commit.
- A copy of this repository's source, installers, helpers and documentation as they existed at backup time.

**Excluded:** movie/video files, torrent payloads, `.torrent` files, earlier backup archives and transient locks/process files. App logs and artwork are included with the app data. No media mount is traversed. Directory symlinks are recorded as references rather than followed into storage. Databases contain movie *records*, which are needed for an identical library, not the movie files themselves.

Appbox-managed qBittorrent/FTP application files are outside this Ubuntu VM and cannot be backed up by this script. The Radarr-side qBittorrent connection is included. Appbox storage, provider mount provisioning, DNS, hostnames and the FTP accounts are not recreated by installing this repository. Host-specific proxy/mount/cron files are references, not automatically activated on another VPS.

## Replacement VPS

Use Ubuntu 24.04 or newer, with the same CPU architecture and a C library at least as new as the source. Mount your existing movie and download storage first. The snapshot does not upload or copy your films.

1. Copy the encrypted snapshot to the replacement VPS.
2. Stop the old Telegram bot before starting the replacement: `media-server stop` on the old VPS. One token must have one polling instance.
3. Install dependencies and restore onto a **fresh** VPS:

```bash
curl -fsSL https://raw.githubusercontent.com/waqaarhussain/media-server/main/bootstrap.sh -o /tmp/media-server-bootstrap.sh && bash /tmp/media-server-bootstrap.sh --user appbox --restore /path/to/media-server-backup.tar.gz.gpg
```

4. If Movies/Kids Movies paths changed, run `media-server paths`. It prompts for the two roots, verifies existing movie files are already at the new paths, then updates Radarr paths without moving files. Finish downloads/deletions before migration. If the download staging mount or qBittorrent host changed, adjust Radarr's remote path mapping/download-client connection too.
5. Run `media-server start`, then `/start` in Telegram.

Restore preserves captured app versions/settings and bot code. `update-live` subsequently installs the latest bot. Restoring refuses to overwrite existing app configurations. Restored Radarr/Prowlarr bind to localhost until the new host's proxy is configured. Review host reference copies in the backup before applying old hostnames, users, mounts or certificates.

For direct administrative access without a public proxy, use SSH tunnels from your PC:

```bash
ssh -L 7878:127.0.0.1:7878 -L 9696:127.0.0.1:9696 appbox@YOUR_VPS
```

Then open `http://localhost:7878` and `http://localhost:9696/prowlarr`. Existing Appbox proxy configuration is not changed during adoption.

## Blank setup without a snapshot

Bootstrap can install native Radarr/Prowlarr on a blank VPS, but it cannot reconstruct unexported live settings. It stops bot setup until a `4K Balanced` profile exists. Configure the apps, qBittorrent connection and Prowlarr app/indexers through their UIs, then run `media-server setup` to enter Telegram credentials and media folders. Use a snapshot for the exact current setup instead of rebuilding settings by hand.

## Helpers

| Command | Purpose |
| --- | --- |
| `update-live` | Pull main and deploy the latest Telegram bot/services. Preserve app config and tracked downloads. |
| `media-server backup` | Create an encrypted snapshot, excluding movie files. |
| `media-server restore FILE.tar.gz.gpg` | Restore to an otherwise fresh installation. |
| `media-server paths` | Change Movies/Kids Movies paths after storage is mounted. Never move/delete movie files. |
| `media-server setup` | Enter missing bot credentials locally. Existing saved credentials are adopted. |
| `media-server status` | Show the four services and next catalogue refresh. |
| `media-server stop` / `media-server start` | Stop/start Telegram polling during migration. |

`update-live` does not upgrade Radarr/Prowlarr binaries. Use their native update mechanism and take a fresh snapshot afterward. This avoids silently migrating their databases during bot updates.

## Telegram behaviour preserved

- One visible bot panel with Back/Main menu navigation, full movie title buttons and five search results per page.
- Movie posters/descriptions for search/recommendations. Plain alphabetical title/year library.
- Movies/Kids Movies selection; browsing previews are cleaned up; a download needs approval.
- 4K preferred with 1080p fallback, maximum 25 GiB, positive seeders and title-label quality checks. Existing custom-format scoring remains Radarr's responsibility.
- Downloads refresh about every five seconds while the progress view is open, using qBittorrent where available and clearly labelled Radarr fallback.
- Partial-title delete search and explicit, expiring confirmation. Exact torrent/file identity checks and pending deletion verification.
- Genre recommendations including psychological thrillers, synopsis and previous/next. IMDb catalogue refresh daily around 04:00 UK time. New titles still need to meet rating/vote thresholds.

## Validation and maintenance

```bash
PYTHONPATH=bot:tests python3 -m unittest discover -s tests -q
bash -n bootstrap.sh install.sh scripts/update-live scripts/media-server
```

The unit tests cover bot behaviour and migration boundaries. They do not claim a live Appbox deployment or a full VM restore test. Secrets and runtime backups are never committed. IMDb datasets are downloaded by the user's server; they are not redistributed here.

Official installation/backup references: [Servarr installer](https://github.com/Servarr/Wiki/blob/master/servarr/servarr-install-script.sh), [Prowlarr Linux installation](https://wiki.servarr.com/prowlarr/installation/linux), [Radarr backup/restore](https://wiki.servarr.com/radarr/faq#how-do-i-backuprestore-radarr).
