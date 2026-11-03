#!/usr/bin/env python3
"""Install/adopt the media stack and move private runtime snapshots between VPSs."""
import argparse
import datetime
import getpass
import fnmatch
import fcntl
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
CONFIG = Path('/etc/media-server.json')
SERVICES = ('media-request-catalog.timer', 'media-request-catalog.service',
            'media-request-bot.service', 'prowlarr.service', 'radarr.service')
APPS = {'radarr': ('Radarr', 7878, ''), 'prowlarr': ('Prowlarr', 9696, '/prowlarr')}
DEFAULT_MOVIES = '/APPBOX_DATA/storage/Media/Movies'
DEFAULT_KIDS = '/APPBOX_DATA/storage/Media/Kids/Movies'
MEDIA_SUFFIXES = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.ts', '.m2ts', '.iso', '.vob', '.wmv', '.webm', '.torrent', '.part', '.!qb'}


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def atomic(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.new')
    with open(temp, 'w') as out:
        os.chmod(temp, mode)
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
    temp.replace(path)


def active(service):
    return subprocess.run(['systemctl', 'is-active', '--quiet', service]).returncode == 0


def account(name=None):
    if CONFIG.exists():
        data = json.loads(CONFIG.read_text())
        if name and name != data['user']:
            raise ValueError('Existing installation uses a different service user.')
        name = data['user']
    name = name or os.environ.get('SUDO_USER') or 'appbox'
    if name == 'root':
        name = 'appbox'
    if not re.fullmatch(r'[a-z_][a-z0-9_-]*', name):
        raise ValueError('Invalid service username.')
    try:
        user = pwd.getpwnam(name)
    except KeyError:
        run(['useradd', '--create-home', '--shell', '/bin/bash', name])
        user = pwd.getpwnam(name)
    home = Path(user.pw_dir)
    if any(c.isspace() or c in '%\\"' for c in str(home)):
        raise ValueError('Service home must have a simple absolute path.')
    data = {'user': name, 'group': grp.getgrgid(user.pw_gid).gr_name,
            'home': str(home), 'uid': user.pw_uid, 'gid': user.pw_gid}
    atomic(CONFIG, json.dumps(data, indent=2) + '\n')
    return data


def own(path, cfg):
    path = Path(path)
    os.chown(path, cfg['uid'], cfg['gid'])
    if path.is_dir():
        for item in path.rglob('*'):
            if not item.is_symlink():
                os.chown(item, cfg['uid'], cfg['gid'])


def snapshot_copy(source, target, excluded=(), binary=False, allowed_root=None):
    """Never follow directory links into media mounts or include movie/torrent payloads."""
    source, target = Path(source), Path(target)
    allowed_root = allowed_root or source.resolve()
    if source.is_symlink():
        atomic(target.with_name(target.name + '.symlink-reference.txt'), os.readlink(source) + '\n')
        return
    if source.is_file():
        if source.suffix.lower() not in MEDIA_SUFFIXES:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if any(fnmatch.fnmatch(item.name, pattern) for pattern in excluded):
            continue
        dest = target / item.name
        if item.is_symlink():
            resolved = item.resolve()
            # Upstream binary bundles sometimes link one library name to another.
            if binary and resolved.is_file() and allowed_root in resolved.parents:
                shutil.copy2(resolved, dest)
            else:
                atomic(dest.with_name(dest.name + '.symlink-reference.txt'), os.readlink(item) + '\n')
        elif item.is_dir():
            snapshot_copy(item, dest, excluded, binary, allowed_root)
        elif item.is_file() and item.suffix.lower() not in MEDIA_SUFFIXES:
            shutil.copy2(item, dest)


def unit(template, cfg, **extra):
    text = (REPO / 'systemd' / template).read_text()
    for key, value in {'USER': cfg['user'], 'GROUP': cfg['group'], 'HOME': cfg['home'], **extra}.items():
        text = text.replace('@' + key + '@', str(value))
    return text


def install_helpers():
    for name in ('media-server', 'update-live'):
        atomic(Path('/usr/local/bin') / name, (REPO / 'scripts' / name).read_text(), 0o755)


def deploy_bot(cfg, start=True):
    home = Path(cfg['home'])
    target = home / '.local/share/media-request-bot'
    state = home / '.local/state/media-request-bot'
    secret = home / '.config/media-request-bot'
    for directory in (target, state, secret):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        os.chown(directory, cfg['uid'], cfg['gid'])
    # Compile before stopping the installed bot. Download, watch and deletion ledgers survive.
    for name in ('bot.py', 'catalog.py'):
        source = (REPO / 'bot' / name).read_text()
        compile(source, name, 'exec')
    was_running = active('media-request-bot.service')
    if was_running:
        run(['systemctl', 'stop', 'media-request-bot.service'])
    for name in ('bot.py', 'catalog.py'):
        if (target / name).exists():
            shutil.copy2(target / name, target / (name + '.previous'))
        atomic(target / name, (REPO / 'bot' / name).read_text())
    own(target, cfg)
    for name in ('media-request-bot.service', 'media-request-catalog.service', 'media-request-catalog.timer'):
        atomic(Path('/etc/systemd/system') / name, unit(name, cfg), 0o644)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'media-request-catalog.timer'])
    configured = all((secret / n).exists() for n in ('telegram.env', 'radarr.env'))
    if configured and start:
        run(['systemctl', 'enable', '--now', 'media-request-bot.service'])
        run(['systemctl', 'is-active', '--quiet', 'media-request-bot.service'])
    return configured


def install_native(cfg):
    machine = platform.machine()
    arch = {'x86_64': 'x64', 'aarch64': 'arm64'}.get(machine)
    if not arch:
        raise ValueError('Only amd64 and arm64 are supported.')
    for lower, (app, port, base) in APPS.items():
        binary = Path('/opt') / app / app
        data = Path('/var/lib') / lower
        # Existing native applications are adopted, never replaced or reconfigured.
        if binary.exists():
            if not (data / 'config.xml').exists():
                raise ValueError(f'{app} exists without its expected data directory. Review manually.')
            continue
        if data.exists() and any(data.iterdir()):
            raise ValueError(f'Existing {app} data found without binaries. Use restore instead of overwriting it.')
        with tempfile.TemporaryDirectory(prefix='media-install-') as temp:
            temp = Path(temp)
            archive = temp / 'app.tar.gz'
            url = f'https://{lower}.servarr.com/v1/update/master/updatefile?os=linux&runtime=netcore&arch={arch}'
            run(['curl', '--fail', '--location', '--retry', '3', '--output', archive, url])
            with tarfile.open(archive) as tar:
                safe_extract(tar, temp / 'unpack')
            source = temp / 'unpack' / app
            if not (source / app).is_file():
                raise ValueError('Downloaded application layout was unexpected.')
            shutil.move(str(source), str(binary.parent))
        own(binary.parent, cfg)
        data.mkdir(parents=True, exist_ok=True)
        data.chmod(0o700)
        root = ET.Element('Config')
        for key, value in {'BindAddress': '127.0.0.1', 'Port': str(port), 'UrlBase': base,
                           'ApiKey': os.urandom(16).hex(), 'AuthenticationMethod': 'External',
                           'AuthenticationRequired': 'DisabledForLocalAddresses',
                           'LaunchBrowser': 'False', 'Branch': 'master', 'EnableSsl': 'False'}.items():
            ET.SubElement(root, key).text = value
        atomic(data / 'config.xml', ET.tostring(root, encoding='unicode'))
        own(data, cfg)
        atomic(Path('/etc/systemd/system') / (lower + '.service'),
               unit('arr.service', cfg, APP=app, LOWER=lower), 0o644)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', '--now', lower + '.service'])


def api(lower, endpoint, payload=None, method=None):
    _, port, base = APPS[lower]
    xml = ET.parse(Path('/var/lib') / lower / 'config.xml').getroot()
    key = xml.findtext('ApiKey')
    base = xml.findtext('UrlBase') or ''
    version = 'v3' if lower == 'radarr' else 'v1'
    url = f'http://127.0.0.1:{port}{base}/api/{version}/{endpoint}'
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, body, {'X-Api-Key': key, 'Content-Type': 'application/json'}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read()
            return json.loads(content) if content else None
    except urllib.error.HTTPError as error:
        raise RuntimeError(f'{lower} request failed with HTTP {error.code}; credentials suppressed.') from None


def wait_api(lower):
    for _ in range(60):
        try:
            return api(lower, 'system/status')
        except Exception:
            time.sleep(1)
    raise RuntimeError(lower + ' did not become ready within 60 seconds.')


def env_read(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines() if '=' in line)


def env_write(path, values, cfg):
    if any('\n' in str(v) or '\r' in str(v) for v in values.values()):
        raise ValueError('Configuration values must be single lines.')
    atomic(path, ''.join(f'{k}={v}\n' for k, v in values.items()))
    own(path, cfg)


def prompt(label, default=None, secret=False):
    suffix = f' [{default}]' if default is not None else ''
    value = (getpass.getpass if secret else input)(label + suffix + ': ').strip()
    return value or default or ''


def path_value(label, default, cfg):
    value = prompt(label, default)
    path = Path(value)
    if not path.is_absolute() or str(path) == '/' or '\n' in value:
        raise ValueError('Media folders must be absolute directories below a storage mount.')
    if not path.is_dir():
        raise ValueError('Folder does not exist. Mount/create storage first: ' + value)
    run(['runuser', '-u', cfg['user'], '--', 'test', '-r', path])
    run(['runuser', '-u', cfg['user'], '--', 'test', '-w', path])
    return str(path)


def setup(cfg):
    """New credentials are prompted locally. Adopt existing env files without rewriting them."""
    directory = Path(cfg['home']) / '.config/media-request-bot'
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    own(directory, cfg)
    if (directory / 'radarr.env').exists() and (directory / 'telegram.env').exists():
        print('Saved Telegram/Radarr configuration adopted unchanged.')
        return
    wait_api('radarr')
    profiles = api('radarr', 'qualityprofile')
    profile = next((p for p in profiles if p['name'] == '4K Balanced'), None)
    if not profile:
        raise ValueError('No 4K Balanced profile found. Restore your encrypted snapshot for the identical setup. '
                         'A blank install needs this profile configured in Radarr before media-server setup.')
    movies = path_value('Movies folder', DEFAULT_MOVIES, cfg)
    kids = path_value('Kids movies folder', DEFAULT_KIDS, cfg)
    if movies == kids or Path(movies) in Path(kids).parents or Path(kids) in Path(movies).parents:
        raise ValueError('Movies and Kids Movies must be separate folders.')
    roots = api('radarr', 'rootfolder')
    for path in (movies, kids):
        if not any(r['path'] == path for r in roots):
            api('radarr', 'rootfolder', {'path': path}, 'POST')
    if not (directory / 'telegram.env').exists():
        token = prompt('Telegram bot token', secret=True)
        uid = prompt('Your Telegram numeric user ID')
        chat = prompt('Telegram private chat ID', uid)
        if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token) or not uid.isdigit() or not chat.isdigit():
            raise ValueError('Invalid Telegram token/user/chat ID. Nothing saved.')
        env_write(directory / 'telegram.env', {'TELEGRAM_BOT_TOKEN': token,
                  'TELEGRAM_ALLOWED_USER_ID': uid, 'TELEGRAM_CHAT_ID': chat}, cfg)
    xml = ET.parse('/var/lib/radarr/config.xml').getroot()
    base = xml.findtext('UrlBase') or ''
    env_write(directory / 'radarr.env', {'RADARR_URL': 'http://127.0.0.1:7878' + base,
              'RADARR_API_KEY': xml.findtext('ApiKey'), 'RADARR_PROFILE_ID': profile['id'],
              'RADARR_MOVIES_ROOT': movies, 'RADARR_KIDS_ROOT': kids}, cfg)


def safe_extract(tar, target):
    """Snapshots contain regular files/directories only; never unpack links or path escapes."""
    target = Path(target).resolve()
    members = tar.getmembers()
    for item in members:
        dest = (target / item.name).resolve()
        if (dest != target and target not in dest.parents) or not (item.isfile() or item.isdir()):
            raise ValueError('Unsafe archive entry. Nothing extracted.')
    target.mkdir(parents=True, exist_ok=True)
    tar.extractall(target, members=members, filter='data')


def passphrase(confirm=False):
    value = getpass.getpass('Backup passphrase: ')
    if not value or '\n' in value:
        raise ValueError('A non-empty single-line passphrase is required.')
    if confirm and value != getpass.getpass('Repeat passphrase: '):
        raise ValueError('Passphrases did not match.')
    return (value + '\n').encode()


def revision():
    result = subprocess.run(['git', '-C', str(REPO), 'rev-parse', 'HEAD'],
                            capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()
    exported = REPO / 'SOURCE_REVISION'
    return exported.read_text().strip() if exported.exists() else 'unversioned-source-archive'


def backup(cfg, destination=None):
    home = Path(cfg['home'])
    folder = home / 'media-server-backups'
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    own(folder, cfg)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = Path(destination) if destination else folder / ('media-server-' + stamp + '.tar.gz.gpg')
    if destination.exists():
        raise ValueError('Backup destination exists. Choose a new filename.')
    secret = passphrase(confirm=True)
    versions = {}
    for lower in APPS:
        versions[lower] = api(lower, 'system/status').get('version')
    running = [s for s in SERVICES if active(s)]
    with tempfile.TemporaryDirectory(prefix='media-server-backup-') as temp:
        temp = Path(temp)
        snapshot = temp / 'snapshot'
        snapshot.mkdir(mode=0o700)
        try:
            for service in running:
                run(['systemctl', 'stop', service])
            for lower, (app, _, _) in APPS.items():
                data = Path('/var/lib') / lower
                if not (data / 'config.xml').is_file() or not (data / (lower + '.db')).is_file():
                    raise ValueError('Expected native app configuration is missing: ' + lower)
                snapshot_copy(data, snapshot / lower, ('Backups', '*.pid'))
                # Preserve actual installed versions, not a moving upstream download link.
                snapshot_copy(Path('/opt') / app, snapshot / 'binaries' / app, binary=True)
                with sqlite3.connect(snapshot / lower / (lower + '.db')) as db:
                    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError(lower + ' database check failed.')
            for source, name in ((home / '.config/media-request-bot', 'bot-config'),
                                 (home / '.local/state/media-request-bot', 'bot-state'),
                                 (home / '.local/share/media-request-bot', 'bot-code')):
                if not source.is_dir():
                    raise ValueError('Bot directory missing: ' + str(source))
                snapshot_copy(source, snapshot / name,
                              ('__pycache__', '*.lock', '*.tmp', '*.new', '*.previous'))
            # Host-specific files are preserved for review, not blindly activated on another host.
            reference_paths = ['/etc/nginx', '/etc/letsencrypt', '/etc/fstab', '/etc/media-server.json']
            for service in SERVICES + ('radarr.service', 'prowlarr.service'):
                reference_paths += ['/etc/systemd/system/' + service,
                                    '/etc/systemd/system/' + service + '.d']
            for name in ('media-server', 'update-live'):
                reference_paths.append('/usr/local/bin/' + name)
            for source in dict.fromkeys(reference_paths):
                source = Path(source)
                if source.exists() or source.is_symlink():
                    snapshot_copy(source, snapshot / 'host-reference' / source.relative_to('/'))
            if shutil.which('crontab'):
                for username in (cfg['user'], 'root'):
                    cron = subprocess.run(['crontab', '-u', username, '-l'], capture_output=True, text=True)
                    if cron.returncode == 0:
                        atomic(snapshot / 'host-reference' / ('crontab-' + username + '.txt'), cron.stdout)
            manifest = {'schema': 1, 'created': stamp, 'architecture': platform.machine(),
                        'libc': platform.libc_ver(), 'versions': versions, 'home': cfg['home'],
                        'user': cfg['user'], 'repository_commit': revision()}
            atomic(snapshot / 'manifest.json', json.dumps(manifest, indent=2) + '\n')
            snapshot_copy(REPO, snapshot / 'repository',
                          ('.git', '__pycache__', '*.pyc', '*.gpg', '*.env', 'backups', 'runtime'))
        finally:
            # Restore only services which were actually running, including after a failed copy.
            restart_failed = []
            for service in reversed(running):
                try:
                    run(['systemctl', 'start', service])
                except subprocess.CalledProcessError:
                    restart_failed.append(service)
            if restart_failed:
                raise RuntimeError('Services need attention after snapshot: ' + ', '.join(restart_failed))
        archive = temp / 'snapshot.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            tar.add(snapshot, arcname='snapshot')
        output = destination.with_name(destination.name + '.partial')
        if output.exists():
            raise ValueError('Partial backup filename exists. Choose a new destination.')
        try:
            run(['gpg', '--batch', '--yes', '--no-symkey-cache', '--pinentry-mode', 'loopback', '--passphrase-fd', '0',
                 '--symmetric', '--cipher-algo', 'AES256', '--output', output, archive], input=secret)
            output.chmod(0o600)
            own(output, cfg)
            output.replace(destination)
        finally:
            if output.exists():
                output.unlink()
    print('Encrypted backup saved. Copy this file off the VPS and keep its passphrase separately:')
    print(destination)


def restore(cfg, filename):
    filename = Path(filename).resolve()
    for lower in APPS:
        if (Path('/var/lib') / lower / 'config.xml').exists() or active(lower + '.service'):
            raise ValueError('Restore is for a fresh VPS. Existing application data was not overwritten.')
    home = Path(cfg['home'])
    if (home / '.config/media-request-bot/telegram.env').exists():
        raise ValueError('Existing bot configuration found. Restore stopped.')
    secret = passphrase()
    with tempfile.TemporaryDirectory(prefix='media-server-restore-') as temp:
        temp = Path(temp)
        archive = temp / 'snapshot.tar.gz'
        run(['gpg', '--batch', '--no-symkey-cache', '--pinentry-mode', 'loopback', '--passphrase-fd', '0',
             '--output', archive, '--decrypt', filename], input=secret)
        with tarfile.open(archive) as tar:
            safe_extract(tar, temp / 'unpack')
        snapshot = temp / 'unpack/snapshot'
        manifest = json.loads((snapshot / 'manifest.json').read_text())
        if manifest['schema'] != 1 or manifest['architecture'] != platform.machine():
            raise ValueError('Backup schema/CPU architecture differs. No files restored.')
        def version(v):
            return tuple(int(x) for x in v.split('.') if x.isdigit())
        if version(platform.libc_ver()[1]) < version(manifest['libc'][1]):
            raise ValueError('Target system libraries are older than this snapshot. Use a newer Ubuntu release.')
        # Validate all databases before touching installation paths.
        for lower, (app, _, _) in APPS.items():
            if (Path('/opt') / app).exists():
                raise ValueError('Existing application binaries found. Restore needs a fresh VPS.')
            if not (snapshot / lower / 'config.xml').is_file() or not (snapshot / 'binaries' / app / app).is_file():
                raise ValueError('Incomplete snapshot.')
            with sqlite3.connect(snapshot / lower / (lower + '.db')) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Snapshot database is invalid.')
        state_path = snapshot / 'bot-state/state.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state.get('deletions'):
                raise ValueError('Snapshot contains unfinished deletions. Complete them on the source before migrating.')
            state['session'] = None
            state['delete_prompts'] = {}
            state.pop('ui', None)
            state.pop('view', None)
            atomic(state_path, json.dumps(state))
        # Keep source bindings as reference; restored app APIs start on loopback only.
        # Provider proxy/TLS setup belongs to the new host and is not blindly replayed.
        for lower, (app, _, _) in APPS.items():
            xmlpath = snapshot / lower / 'config.xml'
            xml = ET.parse(xmlpath)
            bind = xml.getroot().find('BindAddress')
            if bind is None:
                bind = ET.SubElement(xml.getroot(), 'BindAddress')
            bind.text = '127.0.0.1'
            xml.write(xmlpath, encoding='unicode')
            shutil.copytree(snapshot / lower, Path('/var/lib') / lower, dirs_exist_ok=True)
            shutil.copytree(snapshot / 'binaries' / app, Path('/opt') / app)
            own(Path('/var/lib') / lower, cfg)
            own(Path('/opt') / app, cfg)
            atomic(Path('/etc/systemd/system') / (lower + '.service'),
                   unit('arr.service', cfg, APP=app, LOWER=lower), 0o644)
        for name, dest in (('bot-config', home / '.config/media-request-bot'),
                           ('bot-state', home / '.local/state/media-request-bot'),
                           ('bot-code', home / '.local/share/media-request-bot')):
            shutil.copytree(snapshot / name, dest, dirs_exist_ok=True)
            dest.chmod(0o700)
            own(dest, cfg)
        run(['systemctl', 'daemon-reload'])
        for lower in APPS:
            run(['systemctl', 'enable', '--now', lower + '.service'])
            wait_api(lower)
    print('Snapshot restored. Keep the source bot stopped before starting the replacement.')
    print('If storage paths changed, run media-server paths before media-server start.')
    # Install the CURRENT repo bot after a separate update; restore keeps the exact snapshot version.
    for name in ('media-request-bot.service', 'media-request-catalog.service', 'media-request-catalog.timer'):
        atomic(Path('/etc/systemd/system') / name, unit(name, cfg), 0o644)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', 'media-request-catalog.timer'])


def remap_path(value, mappings):
    if not isinstance(value, str):
        return value
    for old, new in sorted(mappings, key=lambda p: len(p[0]), reverse=True):
        if value == old or value.startswith(old.rstrip('/') + '/'):
            return new.rstrip('/') + value[len(old.rstrip('/')):]
    return value


def remap_json(value, mappings):
    if isinstance(value, dict):
        return {k: remap_json(v, mappings) for k, v in value.items()}
    if isinstance(value, list):
        return [remap_json(v, mappings) for v in value]
    return remap_path(value, mappings)


def paths(cfg):
    home = Path(cfg['home'])
    envfile = home / '.config/media-request-bot/radarr.env'
    values = env_read(envfile)
    statefile = home / '.local/state/media-request-bot/state.json'
    state = json.loads(statefile.read_text()) if statefile.exists() else {}
    if state.get('deletions') or api('radarr', 'queue?pageSize=1000').get('totalRecords', 0):
        raise ValueError('Finish active downloads and deletions before changing media paths.')
    mappings = []
    for key, label in (('RADARR_MOVIES_ROOT', 'Movies folder'), ('RADARR_KIDS_ROOT', 'Kids movies folder')):
        new = path_value(label, values[key], cfg)
        mappings.append((values[key], new))
    newpaths = [Path(new) for _, new in mappings]
    if newpaths[0] == newpaths[1] or newpaths[0] in newpaths[1].parents or newpaths[1] in newpaths[0].parents:
        raise ValueError('Movies and Kids Movies must be separate folders.')
    if all(old == new for old, new in mappings):
        print('Folders unchanged.')
        return
    # Confirm files are already mounted at their new locations. Never move or delete media.
    movies = api('radarr', 'movie')
    for movie in movies:
        new = remap_path(movie['path'], mappings)
        if new != movie['path'] and movie.get('hasFile'):
            relative = (movie.get('movieFile') or {}).get('relativePath')
            if not relative or not (Path(new) / relative).is_file():
                raise ValueError('Movie file missing at the new mount. No settings changed: ' + new)
    was_running = active('media-request-bot.service')
    if was_running:
        run(['systemctl', 'stop', 'media-request-bot.service'])
    try:
        roots = api('radarr', 'rootfolder')
        for old, new in mappings:
            if old != new and not any(r['path'] == new for r in roots):
                api('radarr', 'rootfolder', {'path': new}, 'POST')
        for movie in movies:
            new = remap_path(movie['path'], mappings)
            if new != movie['path']:
                movie['path'] = new
                api('radarr', 'movie/' + str(movie['id']) + '?moveFiles=false', movie, 'PUT')
        for mapping in api('radarr', 'remotepathmapping'):
            new = remap_path(mapping['localPath'], mappings)
            if new != mapping['localPath']:
                mapping['localPath'] = new
                api('radarr', 'remotepathmapping/' + str(mapping['id']), mapping, 'PUT')
        values['RADARR_MOVIES_ROOT'], values['RADARR_KIDS_ROOT'] = [new for _, new in mappings]
        env_write(envfile, values, cfg)
        if statefile.exists():
            atomic(statefile, json.dumps(remap_json(state, mappings)))
            own(statefile, cfg)
    except Exception:
        print('Folder update did not finish. Bot stays stopped; fix mounts/settings before media-server start.', file=sys.stderr)
        raise
    if was_running:
        run(['systemctl', 'start', 'media-request-bot.service'])
    print('Media paths updated. No movie files were moved or deleted.')
    print('If the qBittorrent mount changed too, update its remote path mappings in Radarr Settings > Download Clients.')


def install(cfg, snapshot=None):
    install_helpers()
    if snapshot:
        restore(cfg, snapshot)
        return
    install_native(cfg)
    try:
        setup(cfg)
    except ValueError as error:
        # Blank install remains usable for app setup; never pretend it matches an unknown source profile.
        deploy_bot(cfg, start=False)
        print(str(error), file=sys.stderr)
        print('Native apps installed. For an identical rebuild use install.sh --restore BACKUP.tar.gz.gpg.', file=sys.stderr)
        return 2
    deploy_bot(cfg)
    print('Installed/adopted. Send /start in Telegram. Run media-server backup to capture the live setup.')
    print('Updates: update-live. Recommendation refresh: daily around 04:00 UK time.')


DOLBY_FORMATS = (
    ('Media Server - Dolby Vision', r'(?<![a-z0-9])(?:DV|DoVi|Dolby[ ._-]?Vision)(?![a-z0-9])'),
    ('Media Server - Dolby Atmos', r'(?<![a-z0-9])Atmos(?![a-z0-9])'),
)


def dolby_quality(cfg):
    """Add optional positive preferences only to the bot's existing quality profile."""
    import copy
    directory = Path(cfg['home']) / '.config/media-request-bot'
    saved = env_read(directory / 'radarr.env')
    profile_id = int(saved['RADARR_PROFILE_ID'])
    profile = api('radarr', 'qualityprofile/' + str(profile_id))
    if profile.get('name') != '4K Balanced':
        raise ValueError('The bot is not using 4K Balanced. No profile settings changed.')
    formats = api('radarr', 'customformat')
    schemas = api('radarr', 'customformat/schema')
    if isinstance(schemas, dict):
        schemas = schemas.get('specifications', [])
    schema = next((s for s in schemas if s.get('implementation') == 'ReleaseTitleSpecification'), None)
    if not schema or not any(f.get('name') == 'value' for f in schema.get('fields', [])):
        raise ValueError('Radarr release-title format schema was not recognised. Nothing changed.')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup_dir = Path(cfg['home']) / '.local/state/media-request-bot/quality-backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.chmod(0o700)
    atomic(backup_dir / (stamp + '.json'), json.dumps({'profile': profile, 'customFormats': formats}, indent=2))
    own(backup_dir, cfg)
    ids = []
    for name, pattern in DOLBY_FORMATS:
        matches = [f for f in formats if f.get('name') == name]
        if len(matches) > 1:
            raise ValueError('Duplicate managed Dolby formats found. Review Radarr Custom Formats.')
        spec = copy.deepcopy(schema)
        spec.update({'name': name, 'negate': False, 'required': False})
        for field in spec['fields']:
            if field.get('name') == 'value':
                field['value'] = pattern
        if matches:
            existing = matches[0]
            specs = existing.get('specifications') or []
            correct = len(specs) == 1 and specs[0].get('implementation') == 'ReleaseTitleSpecification'
            correct = correct and not specs[0].get('negate')
            correct = correct and any(f.get('name') == 'value' and f.get('value') == pattern
                                      for f in specs[0].get('fields', []))
            if not correct:
                raise ValueError('A managed Dolby format has been edited. Nothing overwritten: ' + name)
            resource = existing
        else:
            resource = api('radarr', 'customformat', {
                'name': name, 'includeCustomFormatWhenRenaming': False, 'specifications': [spec]}, 'POST')
            formats.append(resource)
        ids.append(int(resource['id']))
    # Re-read after format creation, retaining every current quality, limit and unrelated score.
    current = api('radarr', 'qualityprofile/' + str(profile_id))
    if current.get('name') != '4K Balanced':
        raise ValueError('Profile changed while configuring Dolby. No profile update submitted.')
    updated = copy.deepcopy(current)
    items = updated.setdefault('formatItems', [])
    for iid, (name, _) in zip(ids, DOLBY_FORMATS):
        item = next((i for i in items if i.get('format') == iid), None)
        if item is None:
            items.append({'format': iid, 'name': name, 'score': 100})
        else:
            item['score'] = 100
    if updated != current:
        api('radarr', 'qualityprofile/' + str(profile_id), updated, 'PUT')
    verified = api('radarr', 'qualityprofile/' + str(profile_id))
    if any(not any(i.get('format') == iid and i.get('score') == 100
                   for i in verified.get('formatItems', [])) for iid in ids):
        raise RuntimeError('Could not verify saved Dolby scores. Review 4K Balanced in Radarr.')
    for key in ('items', 'upgradeAllowed', 'cutoff', 'minFormatScore', 'cutoffFormatScore',
                'minUpgradeFormatScore', 'language'):
        if verified.get(key) != current.get(key):
            raise RuntimeError('Other profile settings changed during verification. Review Radarr before downloading.')
    print('4K Balanced saved: Dolby Vision +100; Dolby Atmos +100; both matching adds +200.')
    print('Optional preferences. Existing qualities, minimum score, limits and upgrade settings preserved.')
    print('Search the movie again in Telegram to refresh the release scores. No downloads were started.')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('install', 'update', 'backup', 'restore', 'paths', 'setup', 'status', 'start', 'stop', 'quality'))
    parser.add_argument('filename', nargs='?')
    parser.add_argument('--restore', dest='snapshot')
    parser.add_argument('--user')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run through media-server, update-live or sudo install.sh.')
    cfg = account(args.user)
    if args.command == 'install':
        return install(cfg, args.snapshot)
    if args.command == 'update':
        install_helpers()
        if not deploy_bot(cfg):
            raise ValueError('Saved credentials missing. Run media-server setup.')
        print('Bot updated. Radarr/Prowlarr settings and download state preserved.')
    elif args.command == 'backup':
        backup(cfg, args.filename)
    elif args.command == 'restore':
        if not args.filename:
            parser.error('restore requires the encrypted backup filename.')
        restore(cfg, args.filename)
    elif args.command == 'paths':
        paths(cfg)
    elif args.command == 'quality':
        dolby_quality(cfg)
    elif args.command == 'setup':
        setup(cfg)
        deploy_bot(cfg)
    elif args.command == 'start':
        run(['systemctl', 'enable', '--now', 'media-request-bot.service'])
    elif args.command == 'stop':
        run(['systemctl', 'stop', 'media-request-bot.service'])
    elif args.command == 'status':
        for service in ('radarr', 'prowlarr', 'media-request-bot', 'media-request-catalog.timer'):
            print(service + ': ' + ('running' if active(service) else 'stopped'))
        run(['systemctl', 'list-timers', 'media-request-catalog.timer', '--no-pager'])
    return 0


if __name__ == '__main__':
    try:
        if os.geteuid() != 0:
            raise ValueError('Run through media-server, update-live or sudo install.sh.')
        with open('/run/lock/media-server-operation.lock', 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Another media-server operation is running.') from None
            raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        # Never dump service responses, token-bearing URLs, config contents or tracebacks.
        print('Media-server operation stopped: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
