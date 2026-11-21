"""Read-only discovery from public MDBList movie lists, shared by all bot users."""
import datetime
import json
import re
import urllib.parse
import urllib.request


class CommunityError(Exception):
    pass


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.load(response)


class MDBList:
    def __init__(self, key, fetch=None):
        self.key = key
        self.fetch = fetch or fetch_json

    def get(self, path, **params):
        # API keys belong only in the private config, never errors/state/logs.
        query = urllib.parse.urlencode(dict(params, apikey=self.key))
        try:
            data = self.fetch('https://api.mdblist.com/'+path+'?'+query)
            if isinstance(data, dict) and any(k in data for k in ('error', 'detail')):
                raise ValueError('API error')
            return data
        except Exception:
            raise CommunityError('MDBList is unavailable, the API key is invalid, or its request limit was reached.') from None

    def verify(self):
        data = self.get('genres')
        if not isinstance(data, list) or not any(isinstance(g, dict) and g.get('slug') for g in data):
            raise CommunityError('MDBList did not return a valid genre list. Existing settings were kept.')

    def picks(self, genre, label):
        search = 'Psychological Thriller' if genre=='psychological' else label
        if genre=='scifi':
            search = 'Science Fiction'
        lists = self.get('lists/search', query=search)
        if not isinstance(lists, list):
            raise CommunityError('MDBList returned an unexpected list response.')
        def number(value):
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0
        lists = [row for row in lists if isinstance(row, dict) and row.get('mediatype')=='movie'
                 and not row.get('private') and number(row.get('id'))>0]
        lists.sort(key=lambda row:number(row.get('likes')), reverse=True)
        pool = {}
        failed = False
        for source in lists[:3]:
            params = {'limit':100, 'offset':0, 'append_to_response':'genres', 'sort':'rank', 'order':'asc'}
            try:
                data = self.get('lists/'+str(number(source['id']))+'/items', **params)
            except CommunityError:
                failed = True
                continue
            if not isinstance(data, dict) or not isinstance(data.get('movies'), list):
                failed = True
                continue
            owner = str(source.get('user_name') or '')[:80]
            slug = str(source.get('slug') or '')[:120]
            url = ('https://mdblist.com/lists/'+owner+'/'+slug
                   if re.fullmatch(r'[A-Za-z0-9_-]+',owner) and re.fullmatch(r'[A-Za-z0-9_-]+',slug) else '')
            for item in data['movies']:
                if not isinstance(item, dict) or item.get('adult') or item.get('mediatype','movie')!='movie':
                    continue
                ids = item.get('ids') if isinstance(item.get('ids'),dict) else {}
                tid = number(ids.get('tmdb') or item.get('id'))
                iid = ids.get('imdb') or item.get('imdb_id') or ''
                if not re.fullmatch(r'tt\d+',str(iid)):
                    iid = ''
                year = number(item.get('release_year'))
                if not item.get('title') or (not tid and not iid) or year>datetime.date.today().year:
                    continue
                identity = ('tmdb',tid) if tid else ('imdb',iid)
                if identity in pool:
                    continue
                movie = {'title':str(item['title'])[:300], 'year':year or '?', 'community':True,
                         'community_list':str(source.get('name') or 'Community movie list')[:150],
                         'community_url':url, 'community_genre':genre}
                if tid:
                    movie['tmdbId'] = tid
                if iid:
                    movie['imdbId'] = iid
                pool[identity] = movie
        if not pool and failed:
            raise CommunityError('MDBList movie lists could not be loaded. Try again later.')
        return list(pool.values())[:300]
