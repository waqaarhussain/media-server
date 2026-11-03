#!/usr/bin/env python3
"""Private, approval-based Telegram movie requests using Radarr v3."""
import difflib
import datetime
import fcntl
import http.cookiejar
import json
import math
import os
import re
import secrets
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo


class ServiceError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def request(url, payload=None, headers=None, timeout=30, method=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", **(headers or {})}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        if error.code == 400:
            try:
                if 'message is not modified' in json.loads(error.read()).get('description','').lower():
                    raise ServiceError('Message unchanged.', status=400) from None
            except (ValueError,TypeError,AttributeError):
                pass
        raise ServiceError(f"Service returned HTTP {error.code}.", status=error.code) from None
    except Exception:
        raise ServiceError("Service connection failed or timed out.") from None


class Qbit:
    """Use Radarr's saved connection in memory; never persist or log credentials."""
    def __init__(self, resource):
        fields = {f['name']: f.get('value') for f in resource.get('fields', [])}
        host = str(fields.get('host') or '')
        if not host or any(x in host for x in ('/', '@', '?', '#')):
            raise ServiceError('Unsupported download-client address.')
        port = int(fields.get('port') or 8080)
        self.origin = ('https' if fields.get('useSsl') else 'http') + '://' + host + ':' + str(port)
        base = str(fields.get('urlBase') or '').strip('/')
        self.url = self.origin + ('/' + base if base else '') + '/api/v2/'
        self.category = str(fields.get('category') or '')
        if not self.category:
            raise ServiceError('A dedicated Radarr torrent category is required for bot deletion.')
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        if self.call('auth/login', {'username': fields.get('username') or '',
                                    'password': fields.get('password') or ''}, raw=True).strip() != 'Ok.':
            raise ServiceError('Download-client login failed. Nothing deleted.')

    def call(self, endpoint, data=None, raw=False):
        body = None if data is None else urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.url + endpoint, data=body, headers={
            'Referer': self.origin, 'Origin': self.origin,
            'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with self.opener.open(req, timeout=25) as response:
                text = response.read().decode()
            return text if raw else json.loads(text)
        except Exception:
            raise ServiceError('Download-client request failed or timed out.') from None

    def torrents(self):
        result = self.call('torrents/info')
        if not isinstance(result, list):
            raise ServiceError('Could not verify download-client contents.')
        return result

    def data_paths(self, torrent):
        # Snapshot exact files before removing the torrent, including incomplete-file suffixes.
        files = self.call('torrents/files?hash='+urllib.parse.quote(torrent['hash']))
        bases = {str(torrent.get(k) or '') for k in ('save_path','download_path')}
        bases.discard('')
        if not bases or not isinstance(files,list):
            raise ServiceError('Cannot identify torrent file locations. Nothing deleted.')
        paths = set()
        for base in bases:
            root = Path(base).resolve()
            for file in files:
                path = (root / file['name']).resolve()
                if root not in path.parents:
                    raise ServiceError('Unexpected torrent file location. Nothing deleted.')
                paths.update((str(path),str(path)+'.!qB'))
        return paths

    def delete(self, torrent_hash):
        if not re.fullmatch(r'(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})', torrent_hash):
            raise ServiceError('Invalid torrent identity.')
        self.call('torrents/delete', {'hashes': torrent_hash, 'deleteFiles': 'true'}, raw=True)
        # qBittorrent removes asynchronously. Refuse to claim completion until a later retry if needed.
        if any(t.get('hash', '').lower() == torrent_hash.lower() for t in self.torrents()):
            raise ServiceError('Torrent removal is still pending. Refresh Downloads before retrying.')


def normal(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def title_score(query, movie):
    q = normal(query)
    names = [movie.get("title", ""), movie.get("originalTitle", "")]
    names += [a.get("title", "") for a in (movie.get("alternateTitles") or [])]
    scores = []
    for name in names:
        n = normal(name)
        similarity = difflib.SequenceMatcher(None, q, n).ratio()
        words = n.split()
        tokens = q.split()
        coverage = sum(max((difflib.SequenceMatcher(None, t, w).ratio()
                            for w in words), default=0) for t in tokens) / max(len(tokens), 1)
        scores.append(max(similarity, coverage * 0.88))
    return max(scores, default=0)


QUALITIES = {
    "Bluray-2160p": 2160, "WEBDL-2160p": 2160, "WEBRip-2160p": 2160,
    "Bluray-1080p": 1080, "WEBDL-1080p": 1080, "WEBRip-1080p": 1080,
}


def eligible(release):
    quality = release.get("quality", {}).get("quality", {}).get("name")
    title = release.get("title") or ""
    return (
        quality in QUALITIES
        and release.get("approved") is True
        and release.get("downloadAllowed") is True
        and not release.get("rejected", False)
        and not release.get("temporarilyRejected", False)
        and not release.get("rejections")
        and 0 < release.get("size", 0) <= 25 * 1024**3
        and release.get("protocol", "").lower() == "torrent"
        and (release.get("seeders") or 0) > 0
        and not re.search(r"\b(?:r[ ._-]?ifftrax|ai[ ._-]?(?:upscale|enhanced)|upscaled|60fps)\b", title, re.I)
    )


def choose_releases(releases):
    good = [r for r in releases if eligible(r)]
    uhd = [r for r in good if QUALITIES[r["quality"]["quality"]["name"]] == 2160]
    chosen = uhd or good
    chosen.sort(key=lambda r: (r.get("customFormatScore", 0), r.get("seeders", 0)), reverse=True)
    unique = {}
    for r in chosen:
        identity = r.get("infoHash") or r.get("guid")
        unique.setdefault(identity, r)
    return list(unique.values())[:15], bool(uhd)


def poster_url(movie):
    candidates = [movie.get('remotePoster')]
    for item in movie.get('images') or []:
        if item.get('coverType') == 'poster':
            candidates.extend((item.get('remoteUrl'), item.get('url')))
    for url in candidates:
        if isinstance(url, str):
            if url.startswith('http://image.tmdb.org/'):
                url = 'https://' + url[len('http://'):]
            if url.startswith('https://'):
                return url
    return None


def merge_movie_metadata(movie, source):
    """Keep Radarr's managed identity/state; fill display metadata for the exact same movie."""
    result = dict(movie)
    if not movie.get('tmdbId') or movie.get('tmdbId') != source.get('tmdbId'):
        return result
    for key in ('remotePoster', 'images', 'overview', 'imdbId', 'runtime', 'genres', 'ratings', 'year'):
        if not result.get(key) and source.get(key):
            result[key] = source[key]
    if not poster_url(result) and poster_url(source):
        result['remotePoster'] = poster_url(source)
    return result


def movie_year(movie):
    try:
        year = int(movie.get('year') or 0)
        return year if year > 0 else 9999
    except (ValueError, TypeError):
        return 9999


def release_search_summary(releases):
    if not releases:
        return 'Your configured indexers returned no download releases for this movie.'
    counts = [
        ('not approved by Radarr', sum(r.get('approved') is not True or r.get('downloadAllowed') is not True
            or bool(r.get('rejected') or r.get('temporarilyRejected') or r.get('rejections')) for r in releases)),
        ('outside the size limit', sum(not 0 < (r.get('size') or 0) <= 25*1024**3 for r in releases)),
        ('no reported seeders', sum((r.get('seeders') or 0) <= 0 for r in releases)),
        ('unsupported quality', sum(r.get('quality', {}).get('quality', {}).get('name') not in QUALITIES for r in releases)),
    ]
    reasons = [f'{count} {label}' for label, count in counts if count]
    return (f'{len(releases)} releases returned. ' + '; '.join(reasons[:3]) +
            ('. Reasons can overlap.' if reasons else '. None passed the current download rules.'))


def release_details(release):
    """Describe title labels conservatively; missing labels stay unknown."""
    title = release.get("title") or ""
    def has(pattern):
        return bool(re.search(pattern, title, re.I))
    quality = release.get("quality", {}).get("quality", {}).get("name", "")
    resolution = {2160: "4K", 1080: "1080p"}.get(QUALITIES.get(quality), "Unknown")
    source = ("Blu-ray" if quality.startswith("Bluray-") else
              "WEB-DL" if quality.startswith("WEBDL-") else
              "WEBRip" if quality.startswith("WEBRip-") else "Unknown")
    vision = has(r"\b(?:DV|DoVi|Dolby[ ._-]?Vision)\b")
    hdr = has(r"\bHDR(?:10(?:\+|Plus)?)?(?=[ ._\-]|$)")
    hdrplus = has(r"\bHDR10(?:\+|Plus)(?=[ ._\-]|$)")
    colour = []
    if vision:
        colour.append("Dolby Vision")
    if hdr:
        colour.append("HDR10+" if hdrplus else "HDR")
    if not colour and has(r"\bSDR\b"):
        colour = ["SDR"]
    codec = ""
    if has(r"\b(?:DDP|DD\+|E[ ._-]?AC[ ._-]?3)(?=[ ._\-0-9]|$)"):
        codec = "Dolby Digital Plus"
    elif has(r"\bTrue[ ._-]?HD(?=[ ._\-0-9]|$)"):
        codec = "Dolby TrueHD"
    elif has(r"\bDTS[ ._-]?HD[ ._-]?MA(?=[ ._\-0-9]|$)"):
        codec = "DTS-HD MA"
    elif has(r"\bDTS(?=[ ._\-0-9]|$)"):
        codec = "DTS"
    elif has(r"\b(?:AC[ ._-]?3|DD)(?=[ ._\-0-9]|$)"):
        codec = "Dolby Digital"
    elif has(r"\bAAC(?=[ ._\-0-9]|$)"):
        codec = "AAC"
    elif has(r"\bOpus(?=[ ._\-0-9]|$)"):
        codec = "Opus"
    channel = re.search(r"(?<!\d)([257])[ ._-]1(?!\d)", title, re.I)
    channels = channel.group(1) + ".1" if channel else ""
    if not channels and has(r"\b6CH\b"):
        channels = "6 channels"
    audio = " ".join(x for x in (codec, channels) if x)
    if has(r"\bAtmos\b"):
        audio = (audio + " · Atmos").strip(" ·")
    return resolution, source, " / ".join(colour) or "Unknown", audio or "Unknown"


GENRES = {
    'action':'Action', 'adventure':'Adventure', 'animation':'Animation', 'comedy':'Comedy',
    'crime':'Crime', 'documentary':'Documentary', 'drama':'Drama', 'family':'Family',
    'fantasy':'Fantasy', 'history':'History', 'horror':'Horror', 'music':'Music',
    'mystery':'Mystery', 'romance':'Romance', 'scifi':'Science Fiction', 'thriller':'Thriller',
    'war':'War', 'western':'Western', 'tvmovie':'TV Movie', 'psychological':'Psychological Thrillers',
}


def rating(movie, source):
    raw = ((movie.get('ratings') or {}).get(source) or {})
    try:
        value,votes = float(raw.get('value') or 0),int(raw.get('votes') or 0)
        maximum = 100 if source=='rottenTomatoes' else 10
        return (value,votes) if 0<value<=maximum and votes>=0 else (0,0)
    except (ValueError,TypeError,OverflowError):
        return 0,0


def recommended(movie, genre):
    if not movie.get('tmdbId') or not movie.get('title') or movie.get('isExcluded'):
        return False
    # Votes are a popularity proxy, never a claim about actual viewing counts.
    imdb,votes = rating(movie,'imdb')
    tomatoes,_ = rating(movie,'rottenTomatoes')
    if imdb:
        if imdb<6.5 or votes<10000:
            return False
    else:
        tmdb,tmdb_votes = rating(movie,'tmdb')
        if tmdb<7 or tmdb_votes<1000:
            return False
    genres = {normal(g) for g in (movie.get('genres') or [])}
    if genre=='psychological':
        plot = ' '.join([movie.get('overview') or '']+[str(k) for k in (movie.get('keywords') or [])])
        return 'thriller' in genres and bool(re.search(
            r'psychological|mind[- ]bending|paranoi|hallucinat|amnesia|dissociat|split personality|'
            r'unreliable narrator|mental (?:illness|institution)|asylum|delusion|disturbing visions|grip on reality',plot,re.I))
    return normal(GENRES.get(genre,'')) in genres


def discovery_movie(movie):
    # Persist only the metadata required to browse and request a recommendation.
    keys = ('title','originalTitle','year','tmdbId','imdbId','runtime','certification','genres',
            'ratings','remotePoster','images','overview','isExcluded','keywords')
    return {k:movie[k] for k in keys if k in movie}


class Bot:
    def __init__(self, config, directory):
        self.c = config
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / "state.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "offset": 0, "jobs": {}, "session": None}
        # Ownership is recorded only for entries added by this version of the bot.
        # Never infer ownership from an old unmonitored entry or its title.
        self.state.setdefault("previews", {})
        self.state.setdefault("watches", {})
        self.state.setdefault("ui", None)
        self.state.setdefault("movie_targets", {})
        self.state.setdefault("delete_prompts", {})
        self.state.setdefault("deletions", {})
        self.user = int(config["TELEGRAM_ALLOWED_USER_ID"])
        self.chat = int(config["TELEGRAM_CHAT_ID"])
        self.last_check = 0
        self.last_live = 0
        self.live_connections = {}
        self.live_cache = {}
        self.live_retry_at = 0

    def save(self):
        temp = self.path.with_suffix(".tmp")
        with open(temp, "w") as file:
            os.chmod(temp, 0o600)
            json.dump(self.state, file)
            file.flush()
            os.fsync(file.fileno())
        temp.replace(self.path)

    def tg(self, method, payload):
        result = request("https://api.telegram.org/bot" + self.c["TELEGRAM_BOT_TOKEN"]
                         + "/" + method, payload, timeout=35)
        if not result.get("ok"):
            raise ServiceError("Telegram request failed.")
        return result["result"]

    def rad(self, endpoint, payload=None, timeout=10, method=None):
        return request(self.c["RADARR_URL"].rstrip("/") + "/api/v3/" + endpoint,
                       payload, {"X-Api-Key": self.c["RADARR_API_KEY"]}, timeout, method)

    def say(self, text, choices=None, status_button=False):
        current = self.state.get('view') or {}
        back = current if current.get('route') in ('list','detail','watchlist') else current.get('back',{'route':'home'})
        return self.panel(text, choices=choices,
                          controls=None if choices else self.back_controls('global:back'),
                          view={'route':'notice','back':back})

    @staticmethod
    def back_controls(target='global:home'):
        return {'inline_keyboard':[[{'text':'‹ Back','callback_data':target},
                                   {'text':'Main menu','callback_data':'global:home'}]]}

    def panel(self, text, choices=None, movie=None, controls=None, view=None, force=False):
        # Add/change the poster inside the existing panel; navigation keeps the same message.
        markup = controls or {'inline_keyboard':[[{
            'text':label, 'callback_data':f"{self.state['session']['nonce']}:{action}"
        }] for label,action in (choices or [])]}
        if choices:
            markup['inline_keyboard'].append([{'text':'Main menu','callback_data':'global:home'}])
        if not markup['inline_keyboard']:
            markup = self.back_controls()
        ui = self.state.get('ui')
        poster = poster_url(movie or {})
        photo = bool(poster)
        if movie and not poster:
            text = text.rstrip() + '\n\nPoster unavailable for this title.'
        text = text[:1000] if photo else text[:4000]
        signature = json.dumps([text,markup,poster],sort_keys=True)
        self.state['view'] = view or {'route':'static'}
        if ui and not (ui.get('photo') and not poster):
            if not force and ui.get('signature')==signature:
                self.save()
                return ui
            payload = {'chat_id':self.chat,'message_id':ui['message_id'],'reply_markup':markup}
            try:
                if poster and (not ui.get('photo') or ui.get('poster')!=poster):
                    self.tg('editMessageMedia',dict(payload,media={'type':'photo','media':poster,'caption':text}))
                    ui.update(photo=True,poster=poster)
                elif ui.get('photo'):
                    self.tg('editMessageCaption',dict(payload,caption=text))
                else:
                    self.tg('editMessageText',dict(payload,text=text,link_preview_options={'is_disabled':True}))
                ui['signature'] = signature
                self.save()
                self.clean_old_panels(ui['message_id'])
                return ui
            except ServiceError as error:
                if str(error)=='Message unchanged.':
                    ui['signature'] = signature
                    self.save()
                    self.clean_old_panels(ui['message_id'])
                    return ui
                if error.status not in (400,404):
                    raise
        # A missing panel gets exactly one replacement. Broken posters fall back to text.
        payload = {'chat_id':self.chat,'reply_markup':markup}
        sent = None
        new_photo = False
        if poster:
            try:
                sent = self.tg('sendPhoto',dict(payload,photo=poster,caption=text))
                new_photo = True
            except ServiceError as error:
                if error.status not in (400,404):
                    raise
        if sent is None:
            if movie and poster:
                text = text.rstrip() + '\n\nPoster could not be loaded.'
            sent = self.tg('sendMessage',dict(payload,text=text,link_preview_options={'is_disabled':True}))
        if isinstance(sent,dict) and sent.get('message_id'):
            self.state['ui'] = {'message_id':sent['message_id'],'photo':new_photo,
                                'poster':poster if new_photo else None,'signature':signature}
            if ui and ui.get('message_id')!=sent['message_id']:
                self.state.setdefault('old_panels',[]).append(ui['message_id'])
            self.save()
            self.clean_old_panels(sent['message_id'])
            return self.state['ui']
        return None

    def clean_old_panels(self, keep):
        ids = set(self.state.pop('old_panels',[])+self.state.pop('list_messages',[])+
                  self.state.pop('alt_messages',[]))
        for job in self.state['jobs'].values():
            old = job.pop('ui',None)
            if old and old.get('message_id'):
                ids.add(old['message_id'])
        for mid in ids-{keep}:
            try:
                self.tg('deleteMessage',{'chat_id':self.chat,'message_id':mid})
            except ServiceError:
                pass
        self.save()

    def retire_panel(self):
        # A new search retires the selection, not the reusable Telegram message.
        return

    @staticmethod
    def movie_identity(movie):
        return {k:movie.get(k) for k in ('id','tmdbId','added','path')}

    def movie_target(self, movie):
        identity = self.movie_identity(movie)
        for token, target in self.state['movie_targets'].items():
            if target == identity:
                return token
        token = secrets.token_hex(8)
        self.state['movie_targets'][token] = identity
        self.save()
        return token

    def progress_controls(self, mid):
        rows = [[{"text":"Check progress","callback_data":"global:job:"+str(mid)}]]
        identity = self.state['jobs'].get(str(mid), {}).get('identity')
        if identity:
            rows.append([{'text':'🗑 Delete movie','callback_data':'global:delete:'+self.movie_target(identity)}])
        rows.append([{'text':'📥 Downloads','callback_data':'global:downloads:0'}])
        rows.append([{'text':'Main menu','callback_data':'global:home'}])
        return {'inline_keyboard':rows}

    @staticmethod
    def home_controls():
        return {'inline_keyboard':[
            [{'text':'🔎 Search new movie','callback_data':'global:search'}],
            [{'text':'🎲 Random Movie','callback_data':'global:genres'}],
            [{'text':'📥 Downloads','callback_data':'global:downloads:0'},
             {'text':'✅ Library','callback_data':'global:library:0'}],
            [{'text':'🗑 Delete a movie','callback_data':'global:manage:0'},
             {'text':'🔔 Watchlist','callback_data':'global:watchlist'}]]}

    def show_home(self, reset=False):
        # Respond using Telegram only. Radarr cleanup runs later, never before the menu appears.
        self.state['session'] = None
        self.state.setdefault('old_panels',[]).extend(self.state.pop('alt_messages',[]))
        if reset:
            old = self.state.get('ui')
            if old:
                self.state['old_panels'].append(old['message_id'])
            self.state['ui'] = None
        self.save()
        updates = list(self.state.get('notices',{}).values())[-3:]
        text = '🎬 Movie menu\n\nWhat are we watching?\nChoose below, or send a movie name.'
        if updates:
            text += '\n\nLatest updates\n'+'\n'.join(updates)
        self.panel(text,controls=self.home_controls(),view={'route':'home'},force=True)

    def notice(self, key, text):
        self.state.setdefault('notices',{})[key] = text
        self.save()

    def restore_view(self, view=None):
        view = view or {'route':'home'}
        if view.get('route')=='delete_results':
            self.search_delete(view.get('query',''),view.get('page',0))
        elif view.get('route')=='delete_search':
            self.prompt_delete()
        elif view.get('route')=='list':
            self.show_downloads(view.get('page',0),library=view.get('mode')=='library',manage=view.get('mode')=='manage')
        elif view.get('route')=='detail':
            self.show_movie(view['token'],view.get('back'))
        elif view.get('route')=='watchlist':
            self.show_watchlist()
        else:
            self.show_home()

    def refresh_visible(self):
        view = self.state.get('view',{})
        if view.get('route') in ('home','list','detail','watchlist'):
            self.restore_view(view)

    def full_queue(self):
        result = self.rad('queue?pageSize=1000&includeUnknownMovieItems=true')
        rows = result.get('records', [])
        if result.get('totalRecords', len(rows)) > len(rows):
            raise ServiceError('Queue is too large to verify safely.')
        return rows

    def live_downloads(self, queue):
        hashes = {str(q.get('downloadId') or '').lower() for q in queue if q.get('downloadId')}
        if not hashes:
            return {}
        now = time.monotonic()
        if now<self.live_retry_at:
            return {}
        if now-self.live_cache.get('time',-100)<4 and self.live_cache.get('hashes')==hashes:
            return self.live_cache['movies']
        connecting = now-self.live_connections.get('time',-100)>300 or not self.live_connections
        try:
            if connecting:
                clients = [Qbit(r) for r in self.rad('downloadclient')
                           if r.get('enable') and r.get('implementation')=='QBittorrent']
                self.live_connections = {'time':now,'clients':clients}
            torrents = {}
            for client in self.live_connections['clients']:
                for torrent in client.torrents():
                    if torrent.get('category')==client.category:
                        torrents[str(torrent.get('hash') or '').lower()] = torrent
            movies = {}
            for item in queue:
                torrent = torrents.get(str(item.get('downloadId') or '').lower())
                if torrent and item.get('movieId'):
                    label = self.qbit_label(torrent)
                    if label:
                        movies[str(item['movieId'])] = label
            self.live_cache = {'time':now,'hashes':hashes,'movies':movies}
            return movies
        except (ServiceError,ValueError,TypeError,KeyError):
            self.live_retry_at = now+(900 if connecting else 60)
            self.live_connections = {}
            self.live_cache = {}
            return {}  # Fall back explicitly to Radarr's queue snapshot, not a stale qBittorrent sample.

    @staticmethod
    def qbit_label(torrent):
        try:
            progress = float(torrent['progress'])
            speed = float(torrent.get('dlspeed') or 0)
            if not math.isfinite(progress) or not math.isfinite(speed) or not 0<=progress<=1:
                return None
        except (KeyError,TypeError,ValueError):
            return None
        state = str(torrent.get('state') or '').lower()
        label = ('Downloaded · waiting for Radarr import' if progress>=1 else
                 'Paused' if state.startswith(('paused','stopped')) else
                 'Stalled' if state=='stalleddl' else 'Checking' if state.startswith('checking') else
                 'Finding metadata' if state in ('metadl','forcedmetadl') else 'Downloading')
        if progress<1:
            label += f' · {progress*100:.1f}%'
        unit,value = ('MiB/s',speed/1024**2) if speed>=1024**2 else ('KiB/s',max(0,speed)/1024)
        label += f'\nSpeed: {value:.1f} {unit}'
        eta = torrent.get('eta')
        if isinstance(eta,(int,float)) and 0<eta<8640000 and progress<1:
            minutes = max(1,math.ceil(eta/60))
            label += f'\nTime left: {minutes//60}h {minutes%60}m' if minutes>=60 else f'\nTime left: {minutes} min'
        return label

    @staticmethod
    def live_stamp(live):
        clock = datetime.datetime.now(ZoneInfo('Europe/London')).strftime('%I:%M:%S %p').lstrip('0')
        return ('qBittorrent live' if live else 'Radarr queue snapshot')+' · Updated '+clock

    def live_view(self):
        view = self.state.get('view') or {}
        return view.get('route')=='detail' or (view.get('route')=='list' and view.get('mode')=='downloads')

    def show_downloads(self, page=0, library=False, manage=False):
        if manage:
            self.prompt_delete()
            return
        movies = {str(m['id']):m for m in self.rad('movie')}
        if not library:
            for mid,pending in self.state['deletions'].items():
                movies.setdefault(mid,dict(pending['identity'],title=pending['title'],year=pending.get('year','?')))
        queue = self.full_queue()
        live = self.live_downloads(queue) if not library else {}
        grouped = {}
        for item in queue:
            grouped.setdefault(str(item.get('movieId')), []).append(item)
        if library:
            ids = [mid for mid,m in movies.items() if m.get('hasFile')]
        else:
            ids = [mid for mid,m in movies.items() if mid in grouped or (
                mid in self.state['jobs'] and not m.get('hasFile') and
                self.state['jobs'][mid].get('status') != 'imported') or mid in self.state['deletions'] or
                (manage and m.get('hasFile'))]
        ids.sort(key=lambda mid: (movies[mid].get('title') or '').casefold())
        if library:
            page = max(0,min(page,max(0,(len(ids)-1)//15)))
            titles = [f"{movies[mid]['title']} ({movies[mid].get('year','?')})" for mid in ids[page*15:page*15+15]]
            rows = []
            nav = []
            if page>0:
                nav.append({'text':'Previous','callback_data':f'global:library:{page-1}'})
            if (page+1)*15<len(ids):
                nav.append({'text':'Next','callback_data':f'global:library:{page+1}'})
            if nav:
                rows.append(nav)
            rows.append([{'text':'🔄 Refresh','callback_data':f'global:library:{page}'},
                         {'text':'‹ Back','callback_data':'global:home'}])
            self.panel(f'✅ Library · {len(ids)} movie(s)\n\n'+('\n'.join(titles) or 'No movies yet.'),
                       controls={'inline_keyboard':rows},view={'route':'list','mode':'library','page':page},force=True)
            return
        page = max(0, min(page, max(0,(len(ids)-1)//2)))
        mode = 'manage' if manage else 'library' if library else 'downloads'
        visible = ids[page*2:page*2+2]
        heading = '🗑 Delete a movie' if manage else '✅ Library' if library else '📥 Downloads'
        lines = [heading+f' · {len(ids)} movie(s)','Tap a movie for details.']
        rows = []
        for mid in visible:
            movie = movies[mid]
            label = ('Ready in your library' if movie.get('hasFile') else
                     live.get(mid) or '\n'.join(self.queue_label(q) for q in grouped.get(mid,[])) or
                     'Not in the queue. Check Radarr before retrying.')
            if mid in self.state['deletions']:
                label = 'Removal pending verification.'
            token = self.movie_target(movie)
            title = f"{movie['title']} ({movie.get('year','?')})"
            short_status = '✅' if movie.get('hasFile') else label.split('\n')[0]
            label = title[:30]+' · '+short_status[:26] if len(short_status)>1 else title[:56]+' '+short_status
            rows.append([{'text':label[:60],'callback_data':'global:item:'+token}])
        if not visible:
            lines.append('No movies to manage.' if manage else 'No imported movies found.' if library else
                         'No active Radarr downloads or pending bot requests.')
        if not library:
            lines.append(self.live_stamp(bool(live))+'\nAuto-refresh about every 5 seconds while open.')
        paging = []
        if page>0:
            paging.append({'text':'Previous','callback_data':f'global:{mode}:{page-1}'})
        if (page+1)*2<len(ids):
            paging.append({'text':'Next','callback_data':f'global:{mode}:{page+1}'})
        if paging:
            lines.append(f'Page {page+1} of {(len(ids)+1)//2}')
            rows.append(paging)
        rows.append([{'text':'🔄 Refresh','callback_data':f'global:{mode}:{page}'},
                     {'text':'‹ Back','callback_data':'global:home'}])
        self.panel('\n\n'.join(lines),controls={'inline_keyboard':rows},
                   view={'route':'list','mode':mode,'page':page},force=True)
        if not library:
            self.last_live = time.monotonic()

    def show_movie(self, token, back=None):
        identity = self.state['movie_targets'].get(token)
        if not identity:
            self.say('That movie button expired. Open Downloads again.')
            return
        back = back or {'route':'list','mode':'downloads','page':0}
        try:
            movie = self.safe_movie(identity)
        except ServiceError as error:
            if error.status==404:
                self.restore_view(back)
                return
            raise
        queue = [q for q in self.full_queue() if q.get('movieId')==movie['id']]
        live = self.live_downloads(queue) if not movie.get('hasFile') else {}
        ready = bool(movie.get('hasFile'))
        label = '' if ready else live.get(str(movie['id'])) or '\n'.join(self.queue_label(q) for q in queue) or 'Not in the active queue.'
        if str(movie['id']) in self.state['deletions']:
            label = 'Removal pending. Refresh to check verification or retry Delete.'
        rows = [[{'text':'🗑 Delete movie','callback_data':'global:delete:'+token}]]
        if not ready:
            label += '\n\n'+self.live_stamp(bool(live))+'\nAuto-refresh about every 5 seconds.'
            rows.append([{'text':'🔄 Refresh','callback_data':'global:detail:'+token}])
        rows.append([{'text':'‹ Back','callback_data':'global:back'},
                     {'text':'Main menu','callback_data':'global:home'}])
        self.panel(f"🎬 {movie['title']} ({movie.get('year','?')})"+('\n\n'+label if label else ''),
                   controls={'inline_keyboard':rows},view={'route':'detail','token':token,'back':back},
                   movie=None if ready else movie,force=True)
        self.last_live = time.monotonic()

    def prompt_delete(self):
        self.end_browse()
        self.panel('🗑 Delete a movie\n\nType any part of its name, or its full title.\n'
                   'I’ll show matching movies, then you can open one and confirm deletion.',
                   controls=self.back_controls(),view={'route':'delete_search'},force=True)

    def search_delete(self, query, page=0):
        query = query.strip()[:150]
        needle = normal(query)
        if not needle:
            self.prompt_delete()
            return
        def matches(movie):
            names = [movie.get('title',''),movie.get('originalTitle','')]
            names += [a.get('title','') for a in (movie.get('alternateTitles') or [])]
            return any(needle in normal(name) or all(t in normal(name) for t in needle.split()) for name in names if name)
        movies = sorted([m for m in self.rad('movie') if matches(m)],
                        key=lambda m:((m.get('title') or '').casefold(),m.get('year',0)))
        page = max(0,min(page,max(0,(len(movies)-1)//7)))
        rows = [[{'text':f"{m['title']} ({m.get('year','?')})"[:60],
                  'callback_data':'global:item:'+self.movie_target(m)}] for m in movies[page*7:page*7+7]]
        nav = []
        if page>0:
            nav.append({'text':'Previous','callback_data':f'global:deletepage:{page-1}'})
        if (page+1)*7<len(movies):
            nav.append({'text':'Next','callback_data':f'global:deletepage:{page+1}'})
        if nav:
            rows.append(nav)
        rows.append([{'text':'Search again','callback_data':'global:manage:0'},
                     {'text':'‹ Back','callback_data':'global:home'}])
        text = f'🗑 Matches for “{query}” · {len(movies)}\n\n'
        text += 'Choose a movie to open its details. Nothing is deleted yet.' if movies else 'No matching movies. Try another part of the name.'
        self.panel(text,controls={'inline_keyboard':rows},
                   view={'route':'delete_results','query':query,'page':page},force=True)

    def catalogue_genres(self):
        path = self.directory/'catalog.sqlite3'
        if path.exists():
            try:
                with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
                    values = {g for (raw,) in db.execute('SELECT DISTINCT genres FROM movies') for g in raw.split(',') if g}
                for name in sorted(values):
                    if name not in ('Adult','Sci-Fi') and normal(name) not in {normal(n) for n in GENRES.values()}:
                        GENRES[normal(name).replace(' ','_')] = name
            except sqlite3.Error:
                pass
        return GENRES

    def catalogue_picks(self, genre, library, queued):
        path = self.directory/'catalog.sqlite3'
        if not path.exists():
            return None
        name = {'scifi':'Sci-Fi'}.get(genre,GENRES[genre])
        params = []
        if genre=='psychological':
            # An editorial theme, since IMDb has no Psychological Thriller genre tag.
            names = ['Shutter Island','Memento','The Game','Black Swan','The Machinist','Fight Club',
                     'Se7en','The Sixth Sense','The Others','Prisoners','Gone Girl','Identity',
                     'Mulholland Drive','Psycho','Vertigo','The Silence of the Lambs',"Jacob's Ladder",
                     'The Invitation','The Gift','Take Shelter','Enemy','Misery','The Invisible Guest',
                     'Perfect Blue','Cure','Coherence','The Vanishing','Frailty','Dead Ringers']
            slots = ','.join('?' for _ in names)
            where = f'(lower(title) IN ({slots}) OR lower(original) IN ({slots}))'
            params = [n.lower() for n in names]*2
        elif genre=='tvmovie':
            where = 'kind=?'
            params = ['tvMovie']
        else:
            where = 'genres LIKE ?'
            params = ['%,'+name+',%']
        excluded = {(m.get('imdbId') or '').lower() for m in library if m.get('hasFile') or m.get('id') in queued or
                    str(m.get('id')) in self.state['jobs'] or str(m.get('id')) in self.state['deletions']}
        try:
            with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
                db.row_factory = sqlite3.Row
                # A Bayesian prior keeps a tiny voting sample from outranking well-established films.
                rows = db.execute('SELECT * FROM movies WHERE '+where+
                    ' AND score>=6.5 AND votes>=10000 ORDER BY (score*votes+6.5*50000)/(votes+50000) DESC LIMIT 1000',params).fetchall()
        except sqlite3.Error:
            return None
        picks = []
        for row in rows:
            if row['imdb'].lower() in excluded:
                continue
            picks.append({'title':row['title'],'originalTitle':row['original'],'year':row['year'],
                          'imdbId':row['imdb'],'genres':row['genres'].strip(',').split(','),
                          'ratings':{'imdb':{'value':row['score'],'votes':row['votes']}},'catalogue':True})
        return picks[:300]

    def hydrate_pick(self, movie):
        if not movie.get('catalogue') or movie.get('hydrated'):
            return movie
        iid = movie['imdbId']
        try:
            results = self.rad('movie/lookup?'+urllib.parse.urlencode({'term':'imdb:'+iid}),timeout=15)
            full = next((m for m in results if str(m.get('imdbId') or '').lower()==iid.lower()),None)
            if full:
                data = discovery_movie(full)
                data.update(catalogue=True,hydrated=True)
                data['ratings'] = dict(data.get('ratings') or {})
                data['ratings']['imdb'] = movie['ratings']['imdb']
                movie.update(data)
        except ServiceError:
            pass
        return movie

    def show_genres(self):
        self.end_browse()
        items = list(self.catalogue_genres().items())
        rows = [[{'text':name,'callback_data':'global:genre:'+key} for key,name in items[i:i+2]]
                for i in range(0,len(items),2)]
        rows.append([{'text':'‹ Back','callback_data':'global:home'}])
        self.panel('🎲 Random Movie\n\nChoose a genre.\n'
                   'IMDb 6.5+ with 10,000+ votes, ranked toward well-established favourites. Rotten Tomatoes is shown when available.\n\n'
                   'Psychological Thrillers is a curated theme. Browsing never starts a download.',
                   controls={'inline_keyboard':rows},view={'route':'genres'},force=True)

    def random_movies(self, genre):
        if genre not in self.catalogue_genres():
            return
        self.end_browse()
        self.panel('🎲 '+GENRES[genre]+'\n\nFinding well-rated movies…',
                   controls=self.back_controls('global:genres'))
        library = self.rad('movie')
        queue = self.full_queue()
        queued = {q.get('movieId') for q in queue}
        excluded = {m.get('tmdbId') for m in library if m.get('hasFile') or m.get('id') in queued or
                    str(m.get('id')) in self.state['jobs'] or str(m.get('id')) in self.state['deletions']}
        deck = self.catalogue_picks(genre,library,queued)
        if deck is None:
            cache = self.state.get('discovery_cache') or {}
            if time.time()-cache.get('saved',0)>21600 or not cache.get('movies'):
                data = self.rad('importlist/movie?includeRecommendations=true&includeTrending=true&includePopular=true',timeout=45)
                if not isinstance(data,list):
                    raise ServiceError('Discovery feed unavailable.')
                pool = list({m['tmdbId']:discovery_movie(m) for m in data if m.get('tmdbId')}.values())[:500]
                cache = {'saved':time.time(),'movies':pool}
                self.state['discovery_cache'] = cache
                self.save()
            deck = [m for m in cache['movies'] if m.get('tmdbId') not in excluded and recommended(m,genre)]
        secrets.SystemRandom().shuffle(deck)
        if not deck:
            self.panel('🎲 '+GENRES[genre]+'\n\nNo matching movies are available in the current catalogue. '
                       'Try another genre or refresh the catalogue by rerunning the update command.',
                       controls=self.back_controls('global:genres'))
            return
        self.state.pop('recommend_return',None)
        self.session('recommend',genre=genre,deck=deck[:100],index=0)
        self.show_random_pick()

    def show_random_pick(self):
        s = self.state['session']
        movie = self.hydrate_pick(s['deck'][s['index']])
        self.save()
        imdb,votes = rating(movie,'imdb')
        tomatoes,_ = rating(movie,'rottenTomatoes')
        rt = f'{tomatoes:g}%' if tomatoes else 'Not available'
        overview = movie.get('overview') or 'No synopsis available.'
        overview = overview[:450]+('…' if len(overview)>450 else '')
        imdb_text = f'{imdb:g}/10 · {votes:,} votes' if imdb else 'Not available'
        text = (f"🎲 {GENRES[s['genre']]} · {s['index']+1}/{len(s['deck'])}\n\n"
                f"🎬 {movie['title']} ({movie.get('year','?')})\n\n"
                f"⭐ IMDb {imdb_text}\n🍅 Rotten Tomatoes: {rt}\n\nAbout the movie\n{overview}\n\n"
                + ('IMDb public catalogue · other details via Radarr.' if movie.get('catalogue') else 'Ratings via Radarr metadata; may lag.'))
        choices = [('Find download','recselect')]
        if s['index']>0:
            choices.append(('Previous','recprev'))
        if s['index']+1<len(s['deck']):
            choices.append(('Next','recnext'))
        choices.append(('‹ Back to genres','recgenres'))
        self.panel(text,choices,movie=movie,view={'route':'recommend'})

    def safe_movie(self, identity):
        movie = self.rad('movie/'+str(identity['id']))
        if self.movie_identity(movie) != identity:
            raise ServiceError('This movie changed. Refresh Downloads and select it again.')
        path = Path(movie.get('path') or '').resolve()
        roots = {Path(self.c[k]).resolve() for k in ('RADARR_MOVIES_ROOT','RADARR_KIDS_ROOT')}
        if path.parent not in roots or path in roots or not movie.get('tmdbId') or not movie.get('added'):
            raise ServiceError('Unexpected movie folder. Remove this movie manually in Radarr.')
        return movie

    def ask_delete(self, token):
        identity = self.state['movie_targets'].get(token)
        if not identity:
            self.say('That movie button expired. Open Downloads again.',status_button=True)
            return
        try:
            movie = self.safe_movie(identity)
        except ServiceError as error:
            pending = self.state['deletions'].get(str(identity['id']))
            if error.status==404 and pending and pending.get('phase')=='removing_radarr':
                self.check_deletions()
                if str(identity['id']) in self.state['deletions']:
                    self.say('Removal is still waiting for file cleanup to be verified. Check Radarr logs if it stays pending.',status_button=True)
                return
            self.say('Movie not available. Refresh Downloads.' if error.status==404 else str(error),status_button=True)
            return
        nonce = secrets.token_hex(8)
        self.state['delete_prompts'] = {nonce:{'identity':identity,'expires':time.time()+120,'back':dict(self.state.get('view') or {'route':'home'})}}
        self.save()
        self.panel(f"🗑 Delete {movie['title']} ({movie.get('year','?')})?\n\n"
                   'Stop its download and remove torrent data, movie files, Radarr entry and bot tracking?\n\n'
                   'Other movies keep downloading. This cannot be undone.',controls={'inline_keyboard':[
            [{'text':'Delete movie + files','callback_data':'global:erase:'+nonce}],
            [{'text':'‹ Back / Keep movie','callback_data':'global:keep:'+nonce}]]},
            view={'route':'delete','back':self.state['delete_prompts'][nonce]['back']},force=True)

    def deletion_plan(self, identity):
        movie = self.safe_movie(identity)
        movies = self.rad('movie')
        path = Path(movie['path']).resolve()
        if any(m['id'] != movie['id'] and (path == Path(m.get('path') or '/').resolve() or
                path in Path(m.get('path') or '/').resolve().parents or
                Path(m.get('path') or '/').resolve() in path.parents) for m in movies):
            raise ServiceError('A library folder is shared with another movie. Nothing deleted.')
        if self.rad('config/mediamanagement').get('recycleBin'):
            raise ServiceError('Radarr has a recycle bin enabled. Permanent deletion needs a manual review; nothing deleted.')
        queue = self.full_queue()
        rows = [q for q in queue if q.get('movieId')==movie['id']]
        history = self.rad('history/movie?movieId='+str(movie['id']))
        hashes = {str(x.get('downloadId') or '').lower() for x in rows+history}
        mid = str(movie['id'])
        hashes.update(self.state['deletions'].get(mid,{}).get('hashes',[]))
        saved_hash = self.state['jobs'].get(mid,{}).get('infoHash')
        if saved_hash:
            hashes.add(str(saved_hash).lower())
        hashes.discard('')
        if any(not re.fullmatch(r'(?:[a-f0-9]{40}|[a-f0-9]{64})',h) for h in hashes):
            raise ServiceError('The download is not an identifiable qBittorrent torrent. Nothing deleted.')
        if not hashes and mid in self.state['jobs']:
            raise ServiceError('No exact torrent identity found for this request. Check Radarr and qBittorrent manually.')
        for h in hashes:
            if any(str(q.get('downloadId','')).lower()==h and q.get('movieId')!=movie['id'] for q in queue):
                raise ServiceError('This torrent is shared with another queue entry. Nothing deleted.')
            events = self.rad('history?pageSize=1000&downloadId='+h)
            records = events.get('records',[])
            if events.get('totalRecords',len(records))>len(records) or any(e.get('movieId')!=movie['id'] for e in records):
                raise ServiceError('Torrent history is shared or incomplete. Nothing deleted.')
        clients = []
        paths = set(self.state['deletions'].get(mid,{}).get('data_paths',[]))
        for resource in self.rad('downloadclient'):
            if not resource.get('enable'):
                continue
            if resource.get('implementation') != 'QBittorrent':
                raise ServiceError('Only qBittorrent clients are supported for full bot deletion.')
            client = Qbit(resource)
            torrents = client.torrents()
            matched = [t for t in torrents if str(t.get('hash','')).lower() in hashes]
            for t in matched:
                if t.get('category') != client.category:
                    raise ServiceError('Torrent category changed. Nothing deleted.')
                content = t.get('content_path')
                if content and any(o.get('hash')!=t.get('hash') and o.get('content_path')==content for o in torrents):
                    raise ServiceError('Torrent files are shared with another torrent. Nothing deleted.')
                paths.update(client.data_paths(t))
            clients.append((client,matched))
        if hashes and not clients:
            raise ServiceError('Cannot verify the download client. Nothing deleted.')
        if any(not q.get('downloadId') for q in rows):
            raise ServiceError('A queue item has no torrent identity. Nothing deleted.')
        return movie,rows,hashes,clients,paths

    def check_deletions(self):
        # Verification only. Background work never initiates or retries destructive requests.
        for mid,pending in list(self.state['deletions'].items()):
            if pending.get('phase')!='removing_radarr':
                continue
            try:
                try:
                    self.rad('movie/'+mid)
                except ServiceError as error:
                    if error.status!=404:
                        raise
                else:
                    continue
                paths = pending.get('data_paths',[]) + [pending['identity']['path']]
                if any(Path(path).exists() for path in paths):
                    continue
                # A disappearing torrent row alone is insufficient when qBittorrent deletes asynchronously.
                clients = [r for r in self.rad('downloadclient') if r.get('enable')]
                if pending['hashes'] and not clients:
                    continue
                if any(r.get('implementation')!='QBittorrent' for r in clients):
                    continue
                if any(str(t.get('hash','')).lower() in pending['hashes']
                       for resource in clients for t in Qbit(resource).torrents()):
                    continue
                self.finish_deletion(mid,pending)
            except (ServiceError,OSError):
                continue

    def finish_deletion(self, mid, pending):
        movie = dict(pending['identity'],title=pending['title'])
        job = self.state['jobs'].pop(mid,{})
        self.state['previews'].pop(mid,None)
        self.state['watches'].pop(str(movie['tmdbId']),None)
        self.state['deletions'].pop(mid,None)
        self.state['movie_targets'] = {k:v for k,v in self.state['movie_targets'].items() if v['id']!=movie['id']}
        if (self.state.get('session') or {}).get('movie',{}).get('id')==movie['id']:
            self.state['session'] = None
        self.save()
        self.notice('deleted:'+mid,f"🗑 {movie['title']} removed")

    def delete_confirmed(self, nonce):
        prompt = self.state['delete_prompts'].pop(nonce,None)
        self.save()  # Consume approval before any network mutation; duplicate taps cannot replay it.
        if not prompt or prompt['expires']<time.time():
            self.say('Deletion confirmation expired. Select the movie again.',status_button=True)
            return
        mid = str(prompt['identity']['id'])
        mutated = False
        try:
            movie,rows,hashes,clients,paths = self.deletion_plan(prompt['identity'])
            self.state['deletions'][mid] = {'identity':prompt['identity'],'hashes':sorted(hashes),
                'title':movie['title'],'year':movie.get('year','?'),'data_paths':sorted(paths)}
            self.state['watches'].pop(str(movie['tmdbId']),None)
            self.save()
            changed = dict(movie,monitored=False)
            mutated = True
            self.rad('movie/'+mid+'?moveFiles=false',changed,method='PUT')
            for client,torrents in clients:
                for torrent in torrents:
                    client.delete(torrent['hash'])
            # qBittorrent already removed the data. Detach Radarr tracking without blocklisting/researching.
            for row in rows:
                try:
                    self.rad('queue/'+str(row['id'])+'?removeFromClient=false&blocklist=false&skipRedownload=true',method='DELETE')
                except ServiceError as error:
                    if error.status!=404:
                        raise
            self.safe_movie(prompt['identity'])
            self.state['deletions'][mid]['phase'] = 'removing_radarr'
            self.save()
            self.rad('movie/'+mid+'?deleteFiles=true&addImportExclusion=false',method='DELETE')
            self.check_deletions()
            if mid in self.state['deletions']:
                self.say(f"🗑 {movie['title']}: removal requested. Waiting for verified file cleanup.",status_button=True)
            else:
                self.say(f"🗑 {movie['title']} removed. Other downloads continue.",status_button=True)
        except ServiceError as error:
            prefix = 'Removal is incomplete. Do not submit this movie again yet. ' if mutated else 'Nothing deleted. '
            self.say(prefix+str(error)+'\nRefresh Downloads to review or retry.',status_button=True)

    def publish_job(self, job, text, mid):
        # Keep progress in state. Background polling must never create chat cards or replace a search.
        job['last_text'] = text
        self.save()

    @staticmethod
    def queue_label(item):
        size = item.get("size") or 0
        remaining = item.get("sizeleft")
        status = str(item.get("status") or "Queued").replace("_"," ").capitalize()
        if size > 0 and remaining is not None:
            percentage = max(0,min(100,100*(1-remaining/size)))
            status += f" · {percentage:.0f}%"
            if percentage >= 100:
                status = "Downloaded · waiting for import"
        eta = item.get("estimatedCompletionTime")
        if eta:
            try:
                seconds = datetime.datetime.fromisoformat(eta.replace("Z","+00:00")).timestamp()-time.time()
                if seconds > 0:
                    minutes = max(1,int(seconds/60))
                    status += f"\nEstimated time left: {minutes//60}h {minutes%60}m" if minutes>=60 else f"\nEstimated time left: {minutes} min"
            except (ValueError,TypeError):
                pass
        return status

    def check_downloads(self, only=None):
        jobs = {mid:job for mid,job in self.state["jobs"].items()
                if job.get("status") != "imported" and (only is None or mid==only)}
        if not jobs:
            return
        queue = self.rad("queue?pageSize=1000")
        records = {str(q.get("movieId")):q for q in queue.get("records",[])}
        for mid, job in jobs.items():
            if mid in self.state['deletions']:
                continue
            try:
                movie = self.rad("movie/"+mid)
            except ServiceError as error:
                if error.status == 404:
                    job["status"] = "Removed from Radarr"
                    self.save()
                    continue
                raise
            identity = self.movie_identity(movie)
            if job.get('identity') and job['identity'] != identity:
                job['status'] = 'Movie identity changed; review Radarr'
                self.save()
                continue
            job['identity'] = identity
            if movie.get("hasFile") and movie.get("movieFileId",0):
                text = f"✅ {job['title']} is ready\n\nImported into your library.\n\nRefresh Infuse if it hasn’t appeared yet."
                self.publish_job(job,text,mid)
                if not job.get("ready_notified"):
                    self.notice("ready:"+mid,"✅ "+job["title"]+" is ready")
                    job["ready_notified"] = True
                job["status"] = "imported"
                self.save()
                continue
            item = records.get(mid)
            if item:
                label = self.queue_label(item)
                warning = str(item.get("trackedDownloadStatus","")).lower() in ("warning","error")
                if warning:
                    label += "\nNeeds attention. Check Radarr Activity for the download/import warning."
                if warning and not job.get("warning_notified"):
                    self.notice("warning:"+mid,f"⚠️ {job['title']} needs attention")
                    job["warning_notified"] = True
                if not warning:
                    job["warning_notified"] = False
            else:
                label = ("Waiting for the download client…" if time.time()-job.get("started",time.time())<300 else
                         "Not currently in the queue. Check Radarr Activity before retrying.")
            job["progress"] = label
            self.publish_job(job,f"⬇️ {job['title']}\n\n{label}",mid)
        self.save()

    def show_watchlist(self):
        watches = self.state['watches']
        lines = ['🔔 Watchlist']
        rows = []
        for tid,w in watches.items():
            title = w['candidate']['title']
            lines.append(title+(' · Version available' if w.get('notified') else ' · Checking availability'))
            if w.get('notified'):
                rows.append([{'text':'View: '+title[:40],'callback_data':'global:watch:'+tid}])
            rows.append([{'text':'Remove: '+title[:40],'callback_data':'global:unwatch:'+tid}])
        if not watches:
            lines.append('Your availability watchlist is empty.')
        rows.append([{'text':'‹ Back','callback_data':'global:home'}])
        self.panel('\n\n'.join(lines),controls={'inline_keyboard':rows},view={'route':'watchlist'},force=True)

    def check_watches(self):
        if self.state.get("session"):
            return  # Give interactive browsing priority over optional background searches.
        due = next(((tid,w) for tid,w in self.state["watches"].items()
                    if not w.get("notified") and w.get("next_check",0)<=time.time()),None)
        if not due:
            return
        tid, watch = due
        watch["next_check"] = time.time()+1800
        self.save()
        candidate = watch["candidate"]
        try:
            existing = self.rad("movie?tmdbId="+tid)
            movie = next((m for m in existing if str(m.get("tmdbId"))==tid),None)
            if movie and (movie.get("hasFile") or str(movie["id"]) in self.state["jobs"]):
                self.state["watches"].pop(tid,None)
                self.save()
                return
            if movie and movie.get("qualityProfileId") != int(self.c["RADARR_PROFILE_ID"]):
                return
            if not movie:
                payload = dict(candidate)
                payload.pop("id",None)
                payload.update({"qualityProfileId":int(self.c["RADARR_PROFILE_ID"]),
                                "rootFolderPath":self.c["RADARR_MOVIES_ROOT"],"monitored":False,
                                "minimumAvailability":"released","addOptions":{"searchForMovie":False}})
                payload.pop("path",None)
                movie = self.rad("movie",payload)
                self.remember_preview(movie)
            records = self.rad("queue?pageSize=1000").get("records",[])
            if any(q.get("movieId")==movie["id"] for q in records):
                self.state["watches"].pop(tid,None)
                self.save()
                return
            choices, uhd = choose_releases(self.rad("release?movieId="+str(movie["id"]),timeout=15))
            if choices:
                self.notice('watch:'+tid,f"🔔 {candidate['title']}: a version is available")
                watch["notified"] = True
                self.save()
        except ServiceError:
            pass
        finally:
            self.cleanup_previews()

    def card(self, text, choices, movie=None):
        return self.panel(text,choices,movie)

    def version_text(self, movie, release, heading=""):
        resolution, source, colour, audio = release_details(release)
        return (f"{heading}\n" if heading else "") + (
            f"🎬 {movie['title']} ({movie.get('year', '?')})\n\n"
            f"📺 {resolution} · {source}\n🎨 {colour}\n🔊 {audio}\n"
            f"💾 {release['size']/1024**3:.1f} GiB\n\n"
            "Audio/HDR based on release labels.")

    def show_recommendation(self, movie, releases, uhd):
        self.show_download_card(movie, releases, 0, uhd, self.c["RADARR_MOVIES_ROOT"])

    def show_download_card(self, movie, releases, selected, uhd, root):
        self.clear_alternatives()
        r = releases[selected]
        self.session("confirm", movie=movie, releases=releases, release=r,
                     selected=selected, uhd=uhd, root=root)
        kids = root == self.c["RADARR_KIDS_ROOT"]
        heading = "Recommended version" if selected == 0 else "Selected alternative"
        if not uhd:
            heading += " · 1080p fallback"
        resolution, source, colour, audio = release_details(r)
        why = f"{resolution}, within your size limit"
        if colour != "Unknown":
            why += ", HDR/colour format labelled" if colour != "SDR" else ", SDR labelled"
        text = self.version_text(movie, r, heading) + (
            f"\n\n📁 {'Kids Movies' if kids else 'Movies'}\n"
            f"Selected for: {why}.\n\nDownload starts only when you approve.")
        self.panel(text, [("Download", "download"),
                          ("Switch to Movies" if kids else "Switch to Kids Movies", "toggle"),
                          ("Other versions", "others:0"), ("Technical details", f"details:{selected}"),
                          ("‹ Back to recommendations" if self.state.get("recommend_return") else "‹ Back to search results",
                           "recback" if self.state.get("recommend_return") else "results")], movie)

    def show_alternatives(self, page):
        self.clear_alternatives()
        s = self.state['session']
        releases = s['releases']
        index = max(1,min(1+page,len(releases)-1))
        if len(releases)<2:
            self.panel('No other eligible versions found.',[('‹ Back','back')], movie=s['movie'])
            return
        choices = [('Use this version',f'use:{index}'),('Technical details',f'details:{index}')]
        if index>1:
            choices.append(('Previous version',f'others:{index-2}'))
        if index+1<len(releases):
            choices.append(('Next version',f'others:{index}'))
        choices += [('‹ Back to selected version','back'),('Main menu','home')]
        self.panel(self.version_text(s['movie'],releases[index],f'Alternative {index} of {len(releases)-1}'),choices,movie=s['movie'])

    def clear_alternatives(self):
        for mid in self.state.pop("alt_messages",[]):
            try:
                self.tg("deleteMessage",{"chat_id":self.chat,"message_id":mid})
            except ServiceError:
                pass
        self.save()

    def session(self, stage, **fields):
        self.state["session"] = {"stage": stage, "nonce": secrets.token_hex(4),
                                  "expires": time.time() + 600, **fields}
        self.save()

    @staticmethod
    def preview_fingerprint(movie):
        keys = ("id", "tmdbId", "path", "added", "qualityProfileId", "monitored", "minimumAvailability")
        return {**{key: movie.get(key) for key in keys}, "tags": sorted(movie.get("tags") or [])}

    def remember_preview(self, movie):
        # The creation timestamp guards against an ID being reused after manual deletion.
        if (not all(movie.get(key) for key in ("id", "tmdbId", "path", "added", "qualityProfileId"))
                or movie.get("monitored") is not False
                or str(Path(movie["path"]).parent) not in
                (self.c["RADARR_MOVIES_ROOT"], self.c["RADARR_KIDS_ROOT"])):
            return
        self.state["previews"][str(movie["id"])] = self.preview_fingerprint(movie)
        self.save()

    def end_browse(self):
        self.clear_alternatives()
        self.state["session"] = None
        self.save()
        self.cleanup_previews()

    def cleanup_previews(self):
        active = self.state.get("session")
        if active and time.time() > active.get("expires", 0):
            self.state["session"] = None
            active = None
            self.save()
        active_id = str((active or {}).get("movie", {}).get("id", ""))
        pending = [mid for mid in self.state["previews"] if mid != active_id]
        if not pending:
            return
        try:
            queue = self.rad("queue?pageSize=1000&includeUnknownMovieItems=true")
            records = queue.get("records")
            if not isinstance(records, list) or queue.get("totalRecords", len(records)) > len(records):
                return  # Incomplete queue information must never authorise deletion.
            if any(not q.get("movieId") for q in records):
                return  # An unmapped download needs manual review before removing previews.
            queued = {str(q["movieId"]) for q in records}
        except Exception:
            return  # Leave the ledger intact and retry after the service recovers.
        for mid in pending:
            if mid in self.state["jobs"] or mid in queued:
                self.state["previews"].pop(mid, None)
                self.save()
                continue
            try:
                movie = self.rad("movie/" + mid)
                original = self.state["previews"][mid]
                if (self.preview_fingerprint(movie) != original or movie.get("hasFile")
                        or movie.get("movieFileId", 0) or movie.get("sizeOnDisk", 0)):
                    # A file or a user change makes this a permanent entry, not our disposable preview.
                    self.state["previews"].pop(mid, None)
                    self.save()
                    continue
                folder = Path(movie["path"])
                if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
                    self.state["previews"].pop(mid, None)
                    self.save()
                    continue
                self.rad("movie/" + mid + "?deleteFiles=false&addImportExclusion=false", method="DELETE")
                self.state["previews"].pop(mid, None)
                self.save()
            except ServiceError as error:
                if error.status == 404:
                    self.state["previews"].pop(mid, None)
                    self.save()
            except Exception:
                pass  # Retain ownership for a later retry, without logging sensitive URLs.

    def find_movies(self, query):
        self.panel("🔎 Finding your movie…")
        found = {}
        terms = [query]
        tokens = [t for t in normal(query).split() if len(t) >= 4 and not t.isdigit()]
        # Broader searches retrieve candidates before fuzzy ranking. No title is auto-selected.
        terms += sorted(set(tokens), key=len, reverse=True)[:2]
        for term in dict.fromkeys(terms):
            movies = self.rad("movie/lookup?" + urllib.parse.urlencode({"term": term}))
            for movie in movies:
                if movie.get("tmdbId"):
                    found[movie["tmdbId"]] = movie
            if term == query and len(movies) >= 8:
                break
        candidates = sorted(found.values(), key=lambda m: title_score(query, m), reverse=True)[:50]
        candidates.sort(key=lambda m: (movie_year(m), -title_score(query, m), normal(m.get('title'))))
        if not candidates:
            self.panel("No movie titles found. Try fewer words or correct the spelling.")
            return
        self.state['search_results'] = candidates
        self.session('movie',movies=candidates)
        self.show_search_results()

    def show_search_results(self, page=0):
        movies = self.state['session']['movies']
        page = max(0,min(page,max(0,(len(movies)-1)//5)))
        chunk = list(enumerate(movies[page*5:page*5+5],page*5))
        choices = [(f"{m['title']} ({m.get('year','?')})",f'movie:{i}') for i,m in chunk]
        if page>0:
            choices.append(('Previous',f'searchpage:{page-1}'))
        if (page+1)*5<len(movies):
            choices.append(('Next',f'searchpage:{page+1}'))
        self.panel('🎬 Which movie?\nOldest release year first.',choices)

    def selected_movie_metadata(self, movie):
        if poster_url(movie) and movie.get('overview'):
            return movie
        term = 'tmdb:' + str(movie['tmdbId'])
        try:
            matches = self.rad('movie/lookup?' + urllib.parse.urlencode({'term': term}), timeout=15)
            if isinstance(matches, list):
                exact = next((m for m in matches if m.get('tmdbId') == movie.get('tmdbId')), None)
                if exact:
                    return merge_movie_metadata(movie, exact)
        except ServiceError:
            pass  # Missing art must not prevent browsing or cause a different film to be selected.
        return movie

    def search_releases(self, candidate):
        if any(p['identity'].get('tmdbId')==candidate.get('tmdbId') for p in self.state['deletions'].values()):
            self.say('This movie has a removal pending. Open Downloads before requesting it again.',status_button=True)
            return
        profile = int(self.c["RADARR_PROFILE_ID"])
        existing = self.rad("movie?" + urllib.parse.urlencode({"tmdbId": candidate["tmdbId"]}))
        movie = next((m for m in existing if m["tmdbId"] == candidate["tmdbId"]), None)
        if movie:
            movie = merge_movie_metadata(movie, candidate)
            if movie.get("hasFile"):
                self.panel(f"✅ {movie['title']} ({movie.get('year','?')})\n\nAlready in your library.", movie=movie)
                return
            if movie["qualityProfileId"] != profile:
                self.say("This movie uses a different quality profile. Set it to 4K Balanced in Radarr first.")
                return
            if str(Path(movie["path"]).parent) not in (self.c["RADARR_MOVIES_ROOT"], self.c["RADARR_KIDS_ROOT"]):
                self.say("This movie is assigned to a folder outside Movies/Kids Movies. Review it in Radarr first.")
                return
        else:
            payload = dict(candidate)
            payload.pop("id", None)
            payload.update({"qualityProfileId": profile, "rootFolderPath": self.c["RADARR_MOVIES_ROOT"],
                            "monitored": False, "minimumAvailability": "released",
                            "addOptions": {"searchForMovie": False}})
            movie = self.rad("movie", payload)
            self.remember_preview(movie)
            movie = merge_movie_metadata(movie, candidate)
        movie_id = str(movie["id"])
        if movie_id in self.state["jobs"]:
            self.panel(f"⬇️ {movie['title']}\n\nAlready requested. Check its progress below.",
                       controls=self.progress_controls(movie_id), movie=movie)
            return
        queue = self.rad("queue?" + urllib.parse.urlencode({"pageSize": 1000, "includeUnknownMovieItems": "false"}))
        if any(q.get("movieId") == movie["id"] for q in queue.get("records", [])):
            self.panel(f"⬇️ {movie['title']}\n\nAlready in your download queue.",
                       controls=self.progress_controls(movie_id), movie=movie)
            return
        self.panel(f"🔎 {movie['title']} ({movie.get('year','?')})\n\nFinding a suitable version…", movie=movie)
        releases = self.rad("release?movieId=" + movie_id, timeout=180)
        chosen, uhd = choose_releases(releases)
        if not chosen:
            self.session("watch_offer", movie=movie)
            self.panel(f"🎬 {movie['title']} ({movie.get('year','?')})\n\n"
                       + ((movie.get('overview') or '')[:200] + '\n\n' if movie.get('overview') else '')
                       + "Movie found. No suitable 4K or 1080p download matched your rules.\n\n"
                       + release_search_summary(releases) + '\n\n'
                       + 'Try another title/year if this is not the film you meant.\n\n'
                       + "Notify you when an eligible version appears? This won’t start a download.",
                       [("Search again", "retry"), ("‹ Back to movie results", "results"),
                        ("Notify me when available", "watch"), ("Cancel", "cancel")],movie)
            return
        self.show_recommendation(movie, chosen, uhd)

    def callback(self, action):
        s = self.state["session"]
        kind, _, number = action.partition(":")
        if kind in ('recnext','recprev') and s['stage']=='recommend':
            s['index'] = max(0,min(len(s['deck'])-1,s['index']+(1 if kind=='recnext' else -1)))
            self.save()
            self.show_random_pick()
        elif kind=='recgenres' and s['stage']=='recommend':
            self.show_genres()
        elif kind=='recselect' and s['stage']=='recommend':
            movie = s['deck'][s['index']]
            self.state['recommend_return'] = dict(s)
            self.state['session'] = None
            self.save()
            if movie.get('tmdbId'):
                self.search_releases(movie)
            else:
                self.find_movies(movie['title']+' '+str(movie.get('year','')))
        elif kind=='recback' and self.state.get('recommend_return'):
            saved = self.state['recommend_return']
            self.end_browse()
            self.session('recommend',genre=saved['genre'],deck=saved['deck'],index=saved['index'])
            self.show_random_pick()
        elif kind == "movie" and s["stage"] == "movie":
            movie = self.selected_movie_metadata(s["movies"][int(number)])
            self.state["session"] = None
            self.save()
            self.search_releases(movie)
        elif kind=='searchpage' and s['stage']=='movie':
            self.show_search_results(int(number))
        elif kind == "back" and s["stage"] == "confirm":
            self.show_download_card(s["movie"], s["releases"], s["selected"], s["uhd"], s["root"])
        elif kind == "toggle" and s["stage"] == "confirm":
            root = self.c["RADARR_KIDS_ROOT"] if s["root"] == self.c["RADARR_MOVIES_ROOT"] else self.c["RADARR_MOVIES_ROOT"]
            self.show_download_card(s["movie"], s["releases"], s["selected"], s["uhd"], root)
        elif kind == "others" and s["stage"] == "confirm":
            self.show_alternatives(max(0, int(number)))
        elif kind == "details" and s["stage"] == "confirm":
            r = s["releases"][int(number)]
            self.panel(f"Technical details\n\n{r['title']}\n\n"
                     f"Source: {r.get('indexer', 'Unknown')}\n"
                     f"Reported seeders: {r.get('seeders', 0)}\n"
                     f"Preference score: {r.get('customFormatScore', 0)}\n\n"
                     "These are release-listing details; the file has not been inspected.",
                     [("Use this version", f"use:{number}"), ("Back", "back")], movie=s['movie'])
        elif kind == "use" and s["stage"] == "confirm":
            selected = int(number)
            s["releases"][selected]  # Validate before changing state.
            self.show_download_card(s["movie"], s["releases"], selected, s["uhd"], s["root"])
        elif kind == 'retry' and s['stage'] == 'watch_offer':
            self.search_releases(self.selected_movie_metadata(s['movie']))
        elif kind == "watch" and s["stage"] == "watch_offer":
            movie = s["movie"]
            tid = str(movie["tmdbId"])
            if len(self.state["watches"]) >= 10 and tid not in self.state["watches"]:
                self.panel("Your watchlist is full. Send /watchlist to remove an entry first.")
                return
            self.state["watches"][tid] = {"candidate":movie,"next_check":time.time()+1800}
            self.save()
            self.end_browse()
            self.panel(f"🔔 Watching for {movie['title']}\n\n"
                       "I’ll check about every 30 minutes while no search menu is active. "
                       "You’ll still approve any download.\n\nUse /watchlist to manage alerts.")
        elif kind == "home":
            self.show_home()
        elif kind == 'results':
            candidates = self.state.get('search_results',[])
            self.end_browse()
            if not candidates:
                self.show_home()
                return
            self.session('movie',movies=candidates)
            self.show_search_results()
        elif kind == "cancel":
            self.end_browse()
            self.panel("Cancelled. Nothing downloaded. Send another movie title.")
        elif kind == "download" and s["stage"] == "confirm":
            movie, selected = s["movie"], s["release"]
            self.clear_alternatives()
            self.panel(f"⏳ {movie['title']}\n\nChecking the selected version before submitting…")
            mid = str(movie["id"])
            if mid in self.state["jobs"]:
                self.say("This movie already has a tracked request. Send /status.")
                return
            current = self.rad("movie/" + mid)
            queue = self.rad("queue?pageSize=1000")
            if (current.get("hasFile") or current.get("path") != movie.get("path")
                    or current.get("qualityProfileId") != int(self.c["RADARR_PROFILE_ID"])
                    or any(q.get("movieId") == movie["id"] for q in queue.get("records", []))):
                self.end_browse()
                self.say("The movie changed or already has a download/file. Review it in Radarr before trying again.")
                return
            # Revalidate instead of force-grabbing a stale or newly rejected result.
            fresh = self.rad("release?movieId=" + str(movie["id"]), timeout=180)
            r = next((r for r in fresh if r.get("guid") == selected.get("guid")
                      and r.get("indexerId") == selected.get("indexerId")), None)
            if not r or not eligible(r) or r.get("size") != selected.get("size"):
                self.end_browse()
                self.say("That release changed or is no longer eligible. Search the title again.")
                return
            root = s["root"]
            if root not in (self.c["RADARR_MOVIES_ROOT"], self.c["RADARR_KIDS_ROOT"]):
                raise ServiceError("Unexpected destination.")
            desired_path = str(Path(root) / Path(current["path"]).name)
            if desired_path != current["path"]:
                # No video file or queued download exists; update only the library destination.
                changed = dict(current)
                changed.update({"path": desired_path, "rootFolderPath": root})
                self.rad("movie/" + mid + "?moveFiles=false", changed, method="PUT")
            job = {"title": movie["title"], "started": time.time(), "status": "submitting",
                   "identity":self.movie_identity(dict(current,path=desired_path)),"infoHash":r.get('infoHash')}
            self.state["jobs"][str(movie["id"])] = job
            self.state["previews"].pop(mid, None)
            self.state["session"] = None
            self.save()  # Record intent before POST; never silently retry an uncertain grab.
            try:
                self.rad("release", {"guid": r["guid"], "indexerId": r["indexerId"],
                                     "movieId": movie["id"]}, timeout=90)
            except ServiceError:
                job["status"] = "check Radarr queue before retrying"
                self.save()
                self.say("Download response was unsuccessful or uncertain. Check Radarr Activity before retrying; I won’t submit it twice.")
                return
            job["status"] = "submitted"
            self.save()
            text = (f"⬇️ {movie['title']} ({movie.get('year', '?')})\n\n"
                    "Download submitted. I’ll let you know once it’s imported and ready for Infuse.")
            ui = self.panel(text, controls=self.progress_controls(mid))
            job["last_text"] = text
            self.state["watches"].pop(str(movie["tmdbId"]),None)
            self.state['view'] = {'route':'detail','token':self.movie_target(dict(current,path=desired_path)),
                                  'back':{'route':'list','mode':'downloads','page':0}}
            self.save()

    def handle(self, update):
        cb = update.get("callback_query")
        message = cb.get("message", {}) if cb else update.get("message", {})
        sender = cb.get("from", {}) if cb else message.get("from", {})
        chat = message.get("chat", {})
        if sender.get("id") != self.user or chat.get("id") != self.chat or chat.get("type") != "private":
            return
        if cb:
            self.tg("answerCallbackQuery", {"callback_query_id": cb["id"]})
            data = cb.get("data","")
            if data=='global:genres':
                self.show_genres()
                return
            if data.startswith('global:genre:'):
                self.random_movies(data.split(':',2)[2])
                return
            if data.startswith('global:deletepage:'):
                self.search_delete((self.state.get('view') or {}).get('query',''),int(data.split(':',2)[2]))
                return
            if data == 'global:home':
                self.show_home()
                return
            if data == 'global:search':
                self.state.pop('recommend_return',None)
                self.end_browse()
                self.panel('🔎 Search a movie\n\nSend its name. Add the year if you know it.',
                           controls={'inline_keyboard':[[{'text':'Main menu','callback_data':'global:home'}]]})
                return
            if data == 'global:watchlist':
                self.show_watchlist()
                return
            if data.startswith(('global:downloads:','global:library:','global:manage:')):
                _,mode,page = data.split(':',2)
                self.check_deletions()
                self.show_downloads(int(page),library=mode=='library',manage=mode=='manage')
                return
            if data.startswith('global:delete:'):
                self.ask_delete(data.split(':',2)[2])
                return
            if data.startswith('global:erase:'):
                self.delete_confirmed(data.split(':',2)[2])
                return
            if data.startswith('global:keep:'):
                kept = self.state['delete_prompts'].pop(data.split(':',2)[2],None)
                self.save()
                self.restore_view(kept.get('back') if kept else {'route':'home'})
                return
            if data=='global:back':
                self.restore_view((self.state.get('view') or {}).get('back'))
                return
            if data.startswith(('global:item:','global:detail:')):
                token = data.split(':',2)[2]
                current = self.state.get('view') or {}
                back = dict(current) if current.get('route') in ('list','delete_results') else current.get('back')
                self.show_movie(token,back)
                return
            if data.startswith('global:job:'):
                mid = data.split(':',2)[2]
                movie = self.rad('movie/'+mid)
                self.show_movie(self.movie_target(movie))
                return
            if data.startswith("global:unwatch:"):
                tid = data.split(":",2)[2]
                self.state["watches"].pop(tid,None)
                self.save()
                self.show_watchlist()
                return
            if data.startswith("global:watch:"):
                tid = data.split(":",2)[2]
                watch = self.state["watches"].get(tid)
                if not watch:
                    self.say("That alert is no longer active. Send the movie title again.")
                    return
                candidate = watch["candidate"]
                self.state["watches"].pop(tid,None)
                self.end_browse()
                self.retire_panel()
                self.search_releases(candidate)
                return
            if cb.get("data") == "global:status":
                self.handle({"message": {"from": sender, "chat": chat, "text": "/status"}})
                return
            nonce, _, action = cb.get("data", "").partition(":")
            s = self.state.get("session")
            if not s or s["nonce"] != nonce or time.time() > s["expires"]:
                self.cleanup_previews()
                self.say("That menu expired. Send the movie title again.")
                return
            s["expires"] = time.time() + 600
            self.save()
            self.callback(action)
            return
        text = message.get("text", "").strip()
        if not text or text.startswith("link-"):
            return
        if text.split()[0].split("@")[0] in ("/start", "/help", "/menu"):
            self.show_home(reset=True)
        elif text == "/cancel":
            self.end_browse()
            self.panel("Current selection cancelled. Submitted downloads continue.")
        elif text == "/watchlist":
            self.show_watchlist()
        elif text in ("/status","/downloads","📥 Downloads"):
            self.check_deletions()
            self.show_downloads()
        elif text in ("/library","✅ Library"):
            self.show_downloads(library=True)
        elif (self.state.get('view') or {}).get('route') in ('delete_search','delete_results') and not text.startswith('/'):
            self.search_delete(text)
        elif text.startswith("/"):
            self.say("Send a movie title, or /help.")
        elif len(text) < 2 or len(text) > 150:
            self.say("Send a movie title between 2 and 150 characters.")
        else:
            self.state.pop('recommend_return',None)
            self.end_browse()
            self.retire_panel()
            self.find_movies(text)

    def check_imports(self):
        self.check_downloads()

    def run(self):
        print("Movie request bot running. Credentials are not logged.", flush=True)
        while True:
            phase = "telegram_poll"
            try:
                updates = self.tg("getUpdates", {"offset": self.state["offset"], "timeout": 1 if self.live_view() else 20,
                                                  "allowed_updates": ["message", "callback_query"]})
                for update in updates:
                    # At-most-once handling prevents replaying side effects after a restart.
                    self.state["offset"] = update["update_id"] + 1
                    self.save()
                    try:
                        phase = "command"
                        self.handle(update)
                    except Exception:
                        print("Request failed; credentials suppressed.", flush=True)
                        self.end_browse()
                        self.say("Request failed. Check Radarr Activity before retrying any approved download. Send /status or try the title again.")
                if not updates and time.time() - self.last_check > 30:
                    self.last_check = time.time()
                    phase = 'preview_cleanup'
                    self.cleanup_previews()
                    phase = 'deletion_verification'
                    self.check_deletions()
                    phase = 'download_check'
                    self.check_imports()
                    phase = 'availability_check'
                    if not self.live_view():
                        self.check_watches()
                    phase = 'menu_refresh'
                    self.refresh_visible()
                if self.live_view() and time.monotonic()-self.last_live>=5:
                    phase = 'live_progress'
                    self.last_live = time.monotonic()
                    self.refresh_visible()
            except Exception as error:
                print(f"Retry: phase={phase}, kind={type(error).__name__}, http={getattr(error,'status',None)}. Credentials suppressed.", flush=True)
                time.sleep(5)


def main():
    os.umask(0o077)
    config = {}
    for name in ("telegram.env", "radarr.env"):
        for line in (Path.home() / ".config/media-request-bot" / name).read_text().splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                config[key] = value
    directory = Path.home() / ".local/state/media-request-bot"
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / "process.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        Bot(config, directory).run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        raise SystemExit("Startup failed. Check configuration and ensure only one bot instance runs.")
