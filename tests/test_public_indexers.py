import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_manage import manage
spec = importlib.util.spec_from_file_location('public_indexers', Path(__file__).resolve().parents[1] / 'scripts/public_indexers.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def source(name='LimeTorrents', **extra):
    return dict({'name': name, 'definitionName': name.lower(), 'implementation': 'Cardigann',
        'privacy': 'public', 'protocol': 'torrent', 'language': 'en-US', 'supportsSearch': True,
        'capabilities': {'categories': [{'id': 2000}]},
        'fields': [{'name': 'definitionFile', 'value': name.lower()}]}, **extra)

class PublicIndexerTests(unittest.TestCase):
    def test_filters(self):
        self.assertTrue(m.candidate(source()))
        self.assertTrue(m.candidate(source(capabilities={'categories': [{'id': 5000}]})))
        for value in ({'privacy': 'private'}, {'privacy': 'semiPrivate'}, {'language': 'ru-RU'},
                      {'language': 'multi'}, {'protocol': 'usenet'}, {'supportsSearch': False},
                      {'capabilities': {'categories': [{'id': 6000}]}}):
            self.assertFalse(m.candidate(source(**value)))

    def test_anime_and_adult_sources_with_tv_categories_are_excluded(self):
        for name in ('E-Hentai', 'Anime Tosho', 'Bangumi Moe', 'nekoBT', 'Nyaa.si',
                     'Shana Project', 'SubsPlease', 'Tokyo Toshokan', 'Unknown Source'):
            self.assertFalse(m.candidate(source(name)), name)
        for name in ('YTS', 'The Pirate Bay', '1337x', 'EZTV', 'showRSS'):
            self.assertTrue(m.candidate(source(name)), name)

    def backend(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cfg = {'home': self.temp.name}
        self.original = source('Knaben', id=1, appProfileId=1)
        self.indexers = [copy.deepcopy(self.original)]
        self.profiles = [{'id': 1, 'name': 'Standard'}]
        self.lost = self.bad = False
        self.writes = []

    def api(self, app, endpoint, payload=None, method=None):
        self.assertEqual(app, 'prowlarr')
        if method: self.writes.append((endpoint, method))
        if endpoint == 'applications':
            return [{'syncLevel': 'fullSync', 'fields': [{'name': 'syncCategories', 'value': [8000] if self.bad else [2000, 5000]}]}]
        if endpoint == 'indexer/schema':
            return [source('Knaben'), source(), source(), source('YTS'), source('Private', privacy='private')]
        if endpoint == 'indexer/test':
            if payload['definitionName'] == 'yts': raise RuntimeError('failed')
            return
        if endpoint == 'appprofile':
            if method == 'POST':
                self.profiles.append(dict(payload, id=2))
                return copy.deepcopy(self.profiles[-1])
            return copy.deepcopy(self.profiles)
        if endpoint == 'indexer':
            if method == 'POST':
                self.indexers.append(dict(copy.deepcopy(payload), id=2))
                if self.lost: raise TimeoutError()
                return copy.deepcopy(self.indexers[-1])
            return copy.deepcopy(self.indexers)
        if endpoint.startswith('indexer/') and method == 'DELETE':
            self.indexers = [r for r in self.indexers if r['id'] != int(endpoint.split('/')[1])]
            return
        if endpoint.startswith('appprofile/') and method == 'DELETE':
            self.profiles = [r for r in self.profiles if r['id'] != int(endpoint.split('/')[1])]
            return
        if endpoint == 'command':
            self.assertEqual(payload, {'name': 'ApplicationIndexerSync'})
            return {'id': 9}
        self.fail(endpoint)

    def helper(self, undo=False):
        with patch('builtins.print'):
            m.operate(self.cfg, self.api, manage.atomic, lambda *_: None, undo)

    def test_repeat_and_undo(self):
        self.backend()
        self.helper()
        self.assertEqual(len(self.indexers), 2)
        self.assertFalse(self.profiles[-1]['enableRss'])
        self.helper()
        self.assertEqual(len(self.indexers), 2)
        manual = source('Manual', id=3, appProfileId=1)
        self.indexers.append(manual)
        self.helper(True)
        self.assertEqual(self.indexers, [self.original, manual])
        self.assertEqual(self.profiles, [{'id': 1, 'name': 'Standard'}])
        self.helper(True)

    def test_lost_post(self):
        self.backend()
        self.lost = True
        with self.assertRaises(ValueError): self.helper()
        self.helper(True)
        self.assertEqual(self.indexers, [self.original])

    def test_edited_refuses_delete(self):
        self.backend()
        self.helper()
        self.indexers[-1]['fields'][0]['value'] = 'changed'
        with self.assertRaises(ValueError): self.helper(True)
        self.assertEqual(len(self.indexers), 2)

    def test_bad_categories_no_mutation(self):
        self.backend()
        self.bad = True
        with self.assertRaises(ValueError): self.helper()
        self.assertEqual(self.writes, [])

    def test_diagnostic_compares_types_and_years_without_mutation(self):
        self.backend()
        target = source('The Pirate Bay', id=16)
        queries = []
        def api(app, endpoint):
            if app == 'radarr' and endpoint == 'indexerstatus':
                return [{'indexerId': 1, 'disabledTill': '2026-11-04T01:00:00Z'}]
            if app == 'radarr':
                self.assertEqual(endpoint, 'indexer')
                return [dict(self.original, enableAutomaticSearch=True, enableInteractiveSearch=True)]
            self.assertEqual((app, endpoint), ('prowlarr', 'indexer'))
            return [target]
        def probe(indexer, kind, query):
            queries.append((indexer, kind, query))
            if query.endswith('2011'):
                return {'rows': []}
            return {'rows': [{'title': 'Insidious 2010 2160p https://private.invalid/key', 'seeders': '47'}]}
        with patch('builtins.print') as printer:
            m.diagnose(api, probe)
        self.assertEqual(queries, [(16, 'search', 'Insidious'), (16, 'movie', 'Insidious'),
                                  (16, 'movie', 'Insidious 2010'), (16, 'movie', 'Insidious 2011')])
        output = ' '.join(str(c) for c in printer.call_args_list)
        self.assertNotIn('private.invalid', output)
        self.assertIn('labelled 4K=1', output)
        self.assertIn('labelled 4K=0', output)
        self.assertEqual(self.writes, [])
