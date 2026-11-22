#!/usr/bin/env python3
"""Read-only qBittorrent settings export and portable, settings-only import helpers."""
import argparse
import datetime
import getpass
import http.cookiejar
import json
import math
import os
from pathlib import Path
import pwd
import subprocess
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# Explicit portable preferences. Host bindings, executable hooks, credentials, RSS
# rules and watched directories need review instead of replaying an AppBox setup.
PORTABLE_KEYS=set('''
locale refresh_interval dht pex lsd encryption anonymous_mode
queueing_enabled max_active_downloads max_active_uploads max_active_torrents
max_active_checking_torrents dont_count_slow_torrents slow_torrent_dl_rate_threshold
slow_torrent_ul_rate_threshold slow_torrent_inactive_timer max_connec max_connec_per_torrent
max_uploads max_uploads_per_torrent dl_limit up_limit alt_dl_limit alt_up_limit
scheduler_enabled schedule_from_hour schedule_from_min schedule_to_hour schedule_to_min
scheduler_days limit_utp_rate limit_tcp_overhead limit_lan_peers utp_tcp_mixed_mode
max_ratio_enabled max_ratio max_ratio_act max_seeding_time_enabled max_seeding_time
max_inactive_seeding_time_enabled max_inactive_seeding_time bittorrent_protocol
preallocate_all incomplete_files_ext use_unwanted_folder file_pool_size checking_memory_use
async_io_threads hashing_threads disk_cache disk_cache_ttl disk_io_type disk_io_read_mode
disk_io_write_mode enable_os_cache coalesce_reads_writes piece_extent_affinity
send_buffer_watermark send_buffer_low_watermark send_buffer_watermark_factor
connection_speed socket_backlog_size outgoing_ports_min outgoing_ports_max
peer_tos connection_protocol tcp_utp_mode utp_rate_limited announce_to_all_tiers
announce_to_all_trackers announce_to_all_trackers_only_when_stopped
stop_tracker_timeout save_resume_data_interval save_statistics_interval
embedded_tracker_port embedded_tracker_port_forwarding embedded_tracker_fastresume
recheck_completed_torrents resolve_peer_countries enable_coalesce_read_write
add_trackers_enabled add_trackers add_trackers_from_url_enabled add_trackers_url
stop_tracker_timeout always_announce_to_all_tiers always_announce_to_all_trackers
idn_support_enabled validate_https_tracker_certificate ssrf_mitigation
create_subfolder_enabled torrent_content_layout auto_delete_mode
start_paused_enabled torrent_stop_condition torrent_added_stopped
subcategories_enabled auto_tmm_enabled torrent_changed_tmm_enabled
save_path_changed_tmm_enabled category_changed_tmm_enabled use_subcategories
export_dir export_dir_fin torrent_files_backup_dir_enabled
'''.split())
# Export directories are intentionally remapped, never copied from the old preferences.
PORTABLE_KEYS-={'export_dir','export_dir_fin','torrent_files_backup_dir_enabled',
               'embedded_tracker_port','embedded_tracker_port_forwarding','embedded_tracker_fastresume',
               'outgoing_ports_min','outgoing_ports_max'}


class Client:
    def __init__(self,url):
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Enter a plain HTTP/HTTPS qBittorrent WebUI URL without credentials or query parameters.')
        self.origin=urllib.parse.urlunsplit((parsed.scheme,parsed.netloc,'','',''))
        self.url=url.rstrip('/')+'/api/v2/'
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def call(self,endpoint,values=None,json_result=False):
        data=None if values is None else urllib.parse.urlencode(values).encode()
        req=urllib.request.Request(self.url+endpoint,data,headers={'Referer':self.origin+'/', 'Origin':self.origin})
        try:
            with self.opener.open(req,timeout=30) as response:body=response.read(8*1024*1024+1)
            if len(body)>8*1024*1024:raise ValueError('Response too large')
            return json.loads(body) if json_result else body.decode()
        except Exception:
            raise ValueError('qBittorrent request failed. Check its address, credentials and connection. Details suppressed.') from None
    def login(self,username,password):
        if self.call('auth/login',{'username':username,'password':password}).strip()!='Ok.':
            raise ValueError('qBittorrent login failed.')


def validate(document):
    if not isinstance(document,dict) or document.get('schema')!=1 or document.get('kind')!='qbit-settings-only':
        raise ValueError('This is not a supported qBittorrent settings export.')
    prefs=document.get('preferences');categories=document.get('categories')
    if not isinstance(prefs,dict) or not isinstance(categories,dict) or len(categories)>500:
        raise ValueError('Invalid qBittorrent preferences/categories.')
    for name,item in categories.items():
        if not isinstance(name,str) or not name or len(name)>200 or any(ord(c)<32 for c in name) or not isinstance(item,dict):
            raise ValueError('Invalid qBittorrent category.')
    return document


def capture(client):
    # No torrents/info, logs, resume files, cookies, RSS articles or torrent payloads.
    return validate({'schema':1,'kind':'qbit-settings-only',
        'created':datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'version':client.call('app/version').strip(),
        'preferences':client.call('app/preferences',json_result=True),
        'categories':client.call('torrents/categories',json_result=True)})


def portable_preferences(document,target):
    validate(document)
    values={};review=[]
    for key,value in document['preferences'].items():
        if key not in PORTABLE_KEYS or key not in target:
            review.append(key);continue
        expected=target[key]
        if isinstance(expected,bool):valid=isinstance(value,bool)
        elif isinstance(expected,(int,float)):
            valid=isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)
        else:valid=isinstance(value,str) and isinstance(expected,str)
        if not valid:review.append(key);continue
        values[key]=value
    # Never import a ratio action that deletes media data. The requested behaviour
    # removes completed torrent entries while keeping the downloaded movie file.
    if values.get('max_ratio_act',0) not in (0,1):
        values['max_ratio_act']=1;review.append('max_ratio_act (delete-files action replaced)')
    return values,sorted(review)


def category_paths(document,pool):
    validate(document)
    # qBittorrent downloads to staging. Radarr imports to Movies or Kids/Movies;
    # pointing a download category directly at a Radarr library root breaks imports.
    aliases={'movies':'Movies','radarr':'Movies','kids':'Kids Movies','kidsmovies':'Kids Movies',
             'tv':'TV Series','tvseries':'TV Series','tvshows':'TV Series',
             'kidstv':'Kids TV Series','kidstvseries':'Kids TV Series','kidstvshows':'Kids TV Series'}
    result={}
    for name in set(document['categories'])|{'Movies','Kids Movies','TV Series','Kids TV Series','radarr'}:
        normalized=''.join(c for c in name.lower() if c.isalnum())
        relative=aliases.get(normalized)
        # Unknown category names are retained, with a neutral new staging path.
        result[name]=str(Path(pool)/'Downloads/Complete'/(relative or 'Other'))
    return dict(sorted(result.items()))


def secret(confirm=False):
    value=getpass.getpass('qBittorrent settings backup encryption passphrase: ')
    if not value:raise ValueError('Passphrase cannot be empty.')
    if len(value.encode())>1024 or '\n' in value or '\r' in value:
        raise ValueError('Use a passphrase of at most 1024 bytes without newlines.')
    if confirm and value!=getpass.getpass('Confirm encryption passphrase: '):raise ValueError('Passphrases differ.')
    return (value+'\n').encode()


def encrypt(document,destination,passphrase):
    destination=Path(destination)
    # Exclusive creation prevents an existing backup from being overwritten.
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    try:
        with os.fdopen(fd,'wb') as out:
            data=json.dumps(validate(document),allow_nan=False).encode()
            # GPG reads the passphrase through an inherited pipe; neither it nor
            # decrypted settings are placed in shell history, argv or a disk file.
            read_fd,write_fd=os.pipe()
            try:
                os.write(write_fd,passphrase);os.close(write_fd);write_fd=None
                result=subprocess.run(['gpg','--batch','--no-symkey-cache','--pinentry-mode','loopback',
                    '--passphrase-fd',str(read_fd),'--symmetric','--cipher-algo','AES256','--output','-'],
                    input=data,stdout=out,stderr=subprocess.PIPE,pass_fds=(read_fd,))
            finally:
                os.close(read_fd)
                if write_fd is not None:os.close(write_fd)
            if result.returncode:raise ValueError('Could not encrypt the qBittorrent settings backup.')
    except BaseException:
        destination.unlink(missing_ok=True);raise


def load_backup(path):
    result=subprocess.run(['gpg','--batch','--no-symkey-cache','--pinentry-mode','loopback',
        '--passphrase-fd','0','--decrypt',str(Path(path).resolve())],input=secret(),capture_output=True)
    if result.returncode:raise ValueError('Cannot decrypt qBittorrent settings. Check the passphrase.')
    if len(result.stdout)>8*1024*1024:raise ValueError('qBittorrent settings export is too large.')
    return validate(json.loads(result.stdout))


def saved_connections():
    try:
        root=ET.parse('/var/lib/radarr/config.xml').getroot()
        port=int(root.findtext('Port') or 7878);base=(root.findtext('UrlBase') or '').rstrip('/')
        req=urllib.request.Request(f'http://127.0.0.1:{port}{base}/api/v3/downloadclient',
                                   headers={'X-Api-Key':root.findtext('ApiKey') or ''})
        with urllib.request.urlopen(req,timeout=15) as response:resources=json.load(response)
        choices=[]
        for resource in resources:
            if resource.get('implementation')!='QBittorrent' or not resource.get('enable'):continue
            fields={f['name']:f.get('value') for f in resource.get('fields',[])}
            host=str(fields.get('host') or '')
            if not host or any(c in host for c in '/@?#'):continue
            host='['+host+']' if ':' in host else host
            url=('https' if fields.get('useSsl') else 'http')+'://'+host+':'+str(int(fields.get('port') or 8080))
            if fields.get('urlBase'):url+='/'+str(fields['urlBase']).strip('/')
            choices.append((str(resource.get('name') or 'qBittorrent'),url,fields.get('username') or '',fields.get('password') or ''))
        return choices
    except Exception:return []


def capture_live(url_override=None):
    choices=[] if url_override else saved_connections()
    # Multiple Radarr categories may reference the same qBittorrent instance.
    choices=list({(url,user):(name,url,user,password) for name,url,user,password in choices}.values())
    if choices:
        print('Saved qBittorrent connections from Radarr:')
        for number,(name,_,_,_) in enumerate(choices,1):print(f'{number}. {name!r}')
        choice='1' if len(choices)==1 else (input('Connection number [1], or M for manual login: ').strip() or '1')
        if choice.lower()=='m':choices=[]
        elif not choice.isdecimal() or not 1<=int(choice)<=len(choices):raise ValueError('Invalid connection number.')
        else:_,url,username,password=choices[int(choice)-1]
    if not choices:
        url=url_override or input('Current qBittorrent WebUI URL (including port/path): ').strip()
        username=input('Current qBittorrent username: ').strip()
        password=getpass.getpass('Current qBittorrent password: ')
    client=Client(url);client.login(username,password)
    return capture(client)


def export(args):
    document=capture_live(args.url)
    home=Path.home();owner=None
    try:
        cfg=json.loads(Path('/etc/media-server.json').read_text())
        home=Path(cfg['home']);owner=pwd.getpwnam(cfg['user'])
    except (OSError,ValueError,KeyError):pass
    folder=home/'media-server-backups';folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    folder.chmod(0o700)
    if owner:os.chown(folder,owner.pw_uid,owner.pw_gid)
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path=folder/('qbit-settings-'+stamp+'.json.gpg')
    encrypt(document,path,secret(confirm=True))
    if owner:os.chown(path,owner.pw_uid,owner.pw_gid)
    print(f'Saved qBittorrent {document["version"]}: {len(document["preferences"])} preferences, {len(document["categories"])} categories.')
    print('No torrents, media files or download history exported. Copy this encrypted file alongside your main backup:')
    print(path)


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['export']);parser.add_argument('--url')
    args=parser.parse_args()
    try:export(args)
    except (ValueError,OSError,TypeError):
        print('qBittorrent export stopped. Check the connection, passphrase and destination. Credentials suppressed.',file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':main()
