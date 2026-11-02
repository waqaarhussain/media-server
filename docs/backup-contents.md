# What to save off the VPS

Save the encrypted `.tar.gz.gpg` file produced by `media-server backup`. Keep its passphrase separately. Copy the archive only after the command prints success; `.partial` files are not complete backups.

The archive is a media-stack snapshot, not a full operating-system disk image. It contains:

```
snapshot/
  manifest.json
  radarr/          database and app configuration
  prowlarr/        database and app configuration
  binaries/        exact installed Radarr and Prowlarr bundles
  bot-config/      saved Telegram/Radarr secrets and folder choices
  bot-state/       library/download/watchlist tracking and recommendation index
  bot-code/        the code actually installed at backup time
  repository/      installer, helpers, documentation and tests at backup time
  host-reference/  service files, overrides, proxy/TLS/mount references and crontabs
```

Radarr/Prowlarr restore their own data, which includes profiles, custom formats, indexers, application links and client configuration. Passwords, private indexer URLs and API keys stay inside the encrypted archive. Movie titles/history are database records, not the actual film files.

The snapshot has no movie folders, downloads directory, qBittorrent payloads, Appbox FTP account store, provider networking state, operating-system package image or provider account credentials. App logs and artwork are included; earlier backup archives and transient locks/process files are omitted. The source binary bundles can make it substantially larger than a settings-only export, but movie sizes do not affect the archive size.

Files under `host-reference` are retained for human review. Automatic restore does not replay a source VPS's fstab, DNS, certificates, Nginx sites or cron tasks on another provider. The snapshot does not create or reconnect a storage mount. Set that up first, then use the preserved reference files where appropriate.

No backup is created in GitHub. Backups are generated only by running the helper on the source VPS and then copying the resulting encrypted file somewhere durable.
