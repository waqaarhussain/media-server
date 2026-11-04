import copy
import importlib.util
import json
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
import year_adapter as y
from test_manage import manage

spec = importlib.util.spec_from_file_location('year_setup', Path(__file__).resolve().parents[1] / 'scripts/year_setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def item(title, iid=None):
    node = ET.Element('item')
    ET.SubElement(node, 'title').text = title
    ET.SubElement(node, 'guid').text = title
    if iid:
        ET.SubElement(node, '{'+y.TORZNAB+'}attr', name='imdb', value=iid)
    return node


class AdapterTests(unittest.TestCase):
    movie = {'id': 7, 'title': 'Insidious', 'originalTitle': 'Insidious',
             'year': 2011, 'imdbId': 'tt1591095', 'tmdbId': 49018}

    def test_metadata_years_exact_id_no_neighbour_guess(self):
        with tempfile.TemporaryDirectory() as home:
            p = Path(home)/'catalog.sqlite3'
            with sqlite3.connect(p) as db:
                db.execute('CREATE TABLE movies (imdb TEXT, year INTEGER, title TEXT, original TEXT)')
                db.executemany('INSERT INTO movies VALUES (?,?,?,?)', [
                    ('tt1591095', 2010, 'Insidious', 'Insidious'),
                    ('tt1234567', 2008, 'Insidious', 'Insidious')])
            years, titles = y.metadata_years(self.movie, p)
            self.assertEqual(years, {2010, 2011})
            self.assertEqual(titles, {'insidious'})
            self.assertEqual(y.metadata_years(dict(self.movie, imdbId='tt9999999'), p)[0], {2011})
            self.assertEqual(y.metadata_years(dict(self.movie, secondaryYear=2009), p)[0], {2009, 2010, 2011})

    def test_release_identity_rejects_sequels_wrong_year_conflicting_ids(self):
        years, titles = {2010, 2011}, {'insidious'}
        self.assertTrue(y.match_item(item('Insidious.2010.2160p.WEB-DL', '1591095'), self.movie, years, titles))
        self.assertTrue(y.match_item(item('Insidious (2010) 2160p'), self.movie, years, titles))
        for node in [item('Insidious 2008 2160p'), item('Insidious Chapter 2 2010 2160p'),
                     item('Insidious 2010 2160p', 'tt1234567'), item('Insidious 2160p'),
                     item('Other Film 2010 2160p'), item('Insidious Chapter 1,2,3,4 2010 2160p')]:
            self.assertFalse(y.match_item(node, self.movie, years, titles))

    def test_adapter_only_searches_verified_alternate_and_preserves_titles(self):
        with tempfile.TemporaryDirectory() as home:
            adapter = y.Adapter({}, Path(home)/'none.sqlite3')
            movie = dict(self.movie, secondaryYear=2010)
            queries = []
            adapter.rad = lambda endpoint: [movie] if endpoint == 'movie' else []
            adapter.sources = lambda movie: [16]
            def feed(source, query):
                queries.append((source, query))
                return [item('Insidious 2010 2160p'), item('Insidious Chapter 2 2010 2160p')]
            adapter.feed = feed
            root = ET.fromstring(adapter.search({'imdbid': ['1591095']}))
            self.assertEqual(queries, [(16, 'Insidious 2010')])
            self.assertEqual([r.findtext('title') for r in root.findall('.//item')], ['Insidious 2010 2160p'])
            self.assertEqual(movie, dict(self.movie, secondaryYear=2010))
            queries.clear()
            self.assertEqual(ET.fromstring(adapter.search({'q': ['Insidious']})).findall('.//item'), [])
            self.assertEqual(queries, [])

    def test_no_verified_alternate_does_not_search(self):
        with tempfile.TemporaryDirectory() as home:
            adapter = y.Adapter({}, Path(home)/'none')
            adapter.rad = lambda endpoint: [self.movie]
            adapter.sources = lambda *_: self.fail('must not query a source')
            self.assertEqual(ET.fromstring(adapter.search({'imdbid': ['1591095']})).findall('.//item'), [])

    def test_sources_only_existing_enabled_prowlarr_connections_and_respects_tags(self):
        adapter = y.Adapter({'prowlarrUrl': 'http://127.0.0.1:9696/prowlarr'}, Path('/no'))
        resource = {'name': 'TPB', 'implementation': 'Torznab', 'enableInteractiveSearch': True,
                    'fields': [{'name': 'baseUrl', 'value': 'http://127.0.0.1:9696/prowlarr/16/'},
                               {'name': 'categories', 'value': [2000]}]}
        wrong = copy.deepcopy(resource)
        wrong['fields'][0]['value'] = 'http://external.invalid/16/'
        tv = copy.deepcopy(resource)
        tv['fields'][1]['value'] = [5000]
        adapter.rad = lambda _: [resource, wrong, tv, dict(resource, enableInteractiveSearch=False),
                                 dict(resource, name=y.NAME), dict(resource, tags=[9])]
        self.assertEqual(adapter.sources(dict(self.movie, tags=[])), [16])

    def test_rss_retains_rejection_markers_for_radarr_and_bot(self):
        text = 'Insidious 2010 2160p AI Upscaled BluRay 60FPS'
        root = ET.fromstring(y.rss([item(text)]))
        self.assertEqual(root.findtext('.//item/title'), text)


class SetupUndoTests(unittest.TestCase):
    def test_undo_only_owned_adapter_keeps_originals(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home)/'.config/media-request-bot/year-adapter.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'adapterKey': 'secret', 'indexerId': 8}))
            resources = [{'id': 1, 'name': 'Knaben'}, {'id': 8, 'name': setup.NAME,
                'implementation': 'Torznab', 'fields': [{'name': 'baseUrl', 'value': setup.BASE},
                                                      {'name': 'apiKey', 'value': 'secret'}]}]
            calls = []
            def api(app, endpoint, payload=None, method=None):
                calls.append((endpoint, method))
                return resources if endpoint == 'indexer' else None
            with patch.object(manage, 'api', api), patch.object(manage, 'run'), patch('builtins.print'):
                setup.operate({'home': home}, manage, undo=True)
            self.assertEqual(calls, [('indexer', None), ('indexer/8', 'DELETE')])
            self.assertFalse(path.exists())

    def test_undo_refuses_replaced_adapter(self):
        with tempfile.TemporaryDirectory() as home:
            path = Path(home)/'.config/media-request-bot/year-adapter.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'adapterKey': 'secret', 'indexerId': 8}))
            with patch.object(manage, 'api', return_value=[{'id': 8, 'name': 'Different Source'}]), patch.object(manage, 'run') as run:
                with self.assertRaises(ValueError): setup.operate({'home': home}, manage, undo=True)
                run.assert_not_called()
            self.assertTrue(path.exists())

class ListenerTests(unittest.TestCase):
    def test_caps_authenticated_loopback_and_rejects_wrong_key(self):
        import urllib.error
        import urllib.request
        with tempfile.TemporaryDirectory() as home:
            p = Path(home)/'config.json'
            p.write_text(json.dumps({'adapterKey': 'test-secret'}))
            try:
                with patch.object(y, 'PORT', 0):
                    server = y.start(p, Path(home)/'catalog.sqlite3')
            except PermissionError:
                self.skipTest('Local sockets unavailable in this runtime')
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            self.assertEqual(server.server_address[0], '127.0.0.1')
            url = 'http://127.0.0.1:' + str(server.server_address[1]) + '/api?t=caps'
            request = urllib.request.Request(url, headers={'X-Api-Key': 'test-secret'})
            with urllib.request.urlopen(request, timeout=2) as response:
                self.assertIn(b'supportedParams="imdbid"', response.read())
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(url, timeout=2)
            self.assertEqual(error.exception.code, 403)

class SetupEnableTests(unittest.TestCase):
    def test_setup_creates_only_rss_disabled_owned_indexer_and_repeated_call_is_idempotent(self):
        import io
        with tempfile.TemporaryDirectory() as home:
            folder = Path(home)/'.config/media-request-bot'
            folder.mkdir(parents=True)
            (folder/'radarr.env').write_text('RADARR_URL=http://127.0.0.1:7878\nRADARR_API_KEY=rad-key\n')
            resources, calls = [], []
            names = ('baseUrl', 'apiPath', 'apiKey', 'categories', 'minimumSeeders', 'removeYear')
            def api(app, endpoint, payload=None, method=None):
                self.assertEqual(app, 'radarr')
                calls.append((endpoint, method))
                if endpoint == 'indexer' and method is None:
                    return resources
                if endpoint == 'indexer/schema':
                    return [{'id': 0, 'implementation': 'Torznab', 'fields': [{'name': n} for n in names]}]
                if endpoint == 'indexer' and method == 'POST':
                    resources.append(dict(copy.deepcopy(payload), id=8))
                    return resources[-1]
                self.fail('Unexpected endpoint or mutation: ' + endpoint)
            class Response(io.BytesIO):
                pass
            xml = ET.ElementTree(ET.fromstring('<Config><ApiKey>prowlarr-key</ApiKey><UrlBase>/prowlarr</UrlBase></Config>'))
            with patch.object(manage, 'api', api), patch.object(manage, 'own'), patch.object(manage, 'deploy_bot'), \
                 patch.object(setup.ET, 'parse', return_value=xml), \
                 patch.object(setup.urllib.request, 'urlopen', side_effect=lambda *_args, **_kw: Response(b'Media Server Verified Years')), \
                 patch('builtins.print'):
                setup.operate({'home': home}, manage)
                setup.operate({'home': home}, manage)
            self.assertEqual(len(resources), 1)
            self.assertFalse(resources[0]['enableRss'])
            self.assertTrue(resources[0]['enableInteractiveSearch'])
            self.assertEqual(sum(method == 'POST' for _, method in calls), 1)
            config = json.loads((folder/'year-adapter.json').read_text())
            self.assertEqual(config['indexerId'], 8)
            self.assertEqual(config['prowlarrUrl'], 'http://127.0.0.1:9696/prowlarr')
            self.assertTrue(setup.owned(resources[0], config))
