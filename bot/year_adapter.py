"""Loopback-only Torznab adapter for metadata-verified alternate release years.

Returns candidates only; Radarr remains responsible for decisions and grabbing.
"""
import concurrent.futures
import copy
import hmac
import json
import re
import sqlite3
import threading
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NAME = 'Media Server Verified Years'
PORT = 18787
TORZNAB = 'http://torznab.com/schemas/2015/feed'
ET.register_namespace('torznab', TORZNAB)


def normal(value):
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', value or '').encode('ascii', 'ignore').decode().lower())


def imdb(value):
    text = str(value or '')
    return 'tt' + text.removeprefix('tt') if re.fullmatch(r'(?:tt)?\d{7,10}', text) else None


def metadata_years(movie, catalogue):
    """Only years attached to this exact movie ID. Never +/- year guessing."""
    years = set()
    for key in ('year', 'secondaryYear'):
        value = movie.get(key)
        if isinstance(value, int) and 1870 <= value <= 2200:
            years.add(value)
    titles = {movie.get('title'), movie.get('originalTitle')}
    iid = imdb(movie.get('imdbId'))
    if iid and catalogue.exists():
        try:
            with sqlite3.connect(catalogue.resolve().as_uri() + '?mode=ro', uri=True, timeout=2) as db:
                row = db.execute('SELECT year,title,original FROM movies WHERE imdb=?', (iid,)).fetchone()
            if row and isinstance(row[0], int) and 1870 <= row[0] <= 2200:
                years.add(row[0])
                titles.update(row[1:])
        except sqlite3.Error:
            pass
    return years, {normal(t) for t in titles if t and normal(t)}


def match_item(item, movie, years, titles):
    title = item.findtext('title') or ''
    match = re.search(r'(?<!\d)(18\d{2}|19\d{2}|20\d{2}|21\d{2})(?!\d)', title)
    if not match or int(match.group()) not in years:
        return False
    prefix = title[:match.start()].strip(' ._-[({')
    if normal(prefix) not in titles:
        return False
    # A standard movie category alone does not establish identity.
    for node in item.iter():
        if node.tag.split('}')[-1] == 'attr' and node.get('name', '').lower() in ('imdb', 'imdbid'):
            if imdb(node.get('value')) != imdb(movie.get('imdbId')):
                return False
        if node.tag.split('}')[-1] == 'attr' and node.get('name', '').lower() in ('tmdb', 'tmdbid'):
            if str(node.get('value')) != str(movie.get('tmdbId')):
                return False
    return True


def rss(items):
    root = ET.Element('rss', version='2.0')
    channel = ET.SubElement(root, 'channel')
    ET.SubElement(channel, 'title').text = NAME
    ET.SubElement(channel, 'description').text = 'Verified alternate release-year candidates'
    for item in items:
        channel.append(copy.deepcopy(item))
    ET.SubElement(channel, '{' + TORZNAB + '}response', offset='0', total=str(len(items)))
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def capabilities():
    # IMDb lookup deliberately avoids matching a requested film by fuzzy title.
    return b'''<?xml version="1.0" encoding="utf-8"?><caps><server title="Media Server Verified Years"/>
<limits max="100" default="100"/><searching><search available="no" supportedParams="q"/>
<movie-search available="yes" supportedParams="imdbid"/></searching>
<categories><category id="2000" name="Movies"><subcat id="2040" name="Movies/HD"/>
<subcat id="2045" name="Movies/UHD"/></category></categories></caps>'''


class Adapter:
    def __init__(self, config, catalogue):
        self.config = config
        self.catalogue = catalogue
        self.lock = threading.BoundedSemaphore(2)

    def rad(self, endpoint):
        url = self.config['radarrUrl'].rstrip('/') + '/api/v3/' + endpoint
        request = urllib.request.Request(url, headers={'X-Api-Key': self.config['radarrKey']})
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)

    def sources(self, movie=None):
        base = self.config['prowlarrUrl'].rstrip('/')
        result = []
        for resource in self.rad('indexer'):
            if not resource.get('enableInteractiveSearch') or resource.get('name') == NAME:
                continue
            if movie and resource.get('tags') and not set(resource['tags']).intersection(movie.get('tags', [])):
                continue
            fields = {f['name']: f.get('value') for f in resource.get('fields', [])}
            url = str(fields.get('baseUrl') or '').rstrip('/')
            if resource.get('implementation') != 'Torznab' or not url.startswith(base + '/'):
                continue
            source_id = url[len(base) + 1:]
            if not source_id.isdigit():
                continue
            # Use only movie-capable connections already enabled in Radarr.
            cats = fields.get('categories') or []
            if not any(2000 <= int(c) < 3000 for c in cats):
                continue
            result.append(int(source_id))
        return list(dict.fromkeys(result))[:6]

    def feed(self, source_id, query):
        params = {'t': 'search', 'cat': '2000', 'limit': 100, 'extended': 1}
        if query:
            params['q'] = query
        url = self.config['prowlarrUrl'].rstrip('/') + '/' + str(source_id) + '/api?' + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={'X-Api-Key': self.config['prowlarrKey']})
        with urllib.request.urlopen(request, timeout=25) as response:
            root = ET.fromstring(response.read())
        if any(n.tag.split('}')[-1] == 'error' for n in root.iter()):
            raise ValueError('Source returned an error')
        return root.findall('.//item')

    def search(self, parameters):
        iid = imdb(parameters.get('imdbid', [''])[0])
        if parameters.get('offset', ['0'])[0] != '0':
            return rss([])
        if not iid:
            if parameters.get('q', [''])[0]:
                return rss([])  # No title-only movie selection.
            # Radarr's initial connection test is a keyword-free feed query.
            # RSS is disabled on this adapter; this never initiates a grab.
            for source_id in self.sources():
                try:
                    items = self.feed(source_id, None)
                    if items:
                        return rss(items[:100])
                except Exception:
                    continue
            return rss([])
        movies = [m for m in self.rad('movie') if imdb(m.get('imdbId')) == iid]
        if len(movies) != 1 or not movies[0].get('tmdbId'):
            return rss([])
        movie = movies[0]
        years, titles = metadata_years(movie, self.catalogue)
        alternatives = sorted(years - {movie.get('year')})
        if not alternatives:
            return rss([])
        sources = self.sources(movie)
        tasks = [(s, movie['title'] + ' ' + str(year)) for s in sources for year in alternatives]
        items, seen = [], set()
        failed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.feed, s, q) for s, q in tasks]
            for future in futures:
                try:
                    rows = future.result()
                except Exception:
                    failed += 1
                    continue
                for row in rows:
                    if not match_item(row, movie, years, titles):
                        continue
                    key = row.findtext('guid') or row.findtext('link') or row.findtext('title')
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(row)
        if tasks and failed == len(tasks):
            raise ValueError('All alternate-year queries failed')
        return rss(items[:100])


def start(config_file, catalogue):
    config = json.loads(config_file.read_text())
    adapter = Adapter(config, catalogue)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Request URLs can contain credentials.

        def do_GET(self):
            parameters = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            key = self.headers.get('X-Api-Key') or parameters.get('apikey', [''])[0]
            if not hmac.compare_digest(key, config['adapterKey']):
                self.send_error(403)
                return
            if urllib.parse.urlsplit(self.path).path != '/api':
                self.send_error(404)
                return
            if not adapter.lock.acquire(blocking=False):
                self.send_error(503)
                return
            try:
                data = capabilities() if parameters.get('t') == ['caps'] else adapter.search(parameters)
                self.send_response(200)
                self.send_header('Content-Type', 'application/rss+xml; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_error(503, 'Search temporarily unavailable')
            finally:
                adapter.lock.release()

    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
