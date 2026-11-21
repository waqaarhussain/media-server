import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import io
from pathlib import Path
from unittest.mock import Mock, patch

from bot import Bot, ServiceError
from community import MDBList, CommunityError, fetch_json
import test_menu
from test_manage import manage


class CommunityClientTests(unittest.TestCase):
    def test_http_requests_use_explicit_application_headers(self):
        with patch('community.urllib.request.urlopen',return_value=io.BytesIO(b'[]')) as fetch:
            self.assertEqual(fetch_json('https://api.mdblist.com/genres/?apikey=private-key'),[])
        request=fetch.call_args.args[0]
        self.assertTrue(request.get_header('User-agent').startswith('media-server/'))
        self.assertEqual(request.get_header('Accept'),'application/json')

    def test_http_errors_report_actual_cause_without_exposing_key(self):
        for code,body,expected in ((403,b'error code: 1010','web firewall'),
                                   (403,b'Forbidden','API permissions'),
                                   (401,b'Invalid API key','authentication'),
                                   (429,b'limit','limit reached'),
                                   (503,b'unavailable','HTTP 503')):
            error=urllib.error.HTTPError('https://api.mdblist.com/?apikey=private-key',
                                         code,'Rejected',{},io.BytesIO(body))
            api=MDBList('private-key',fetch=Mock(side_effect=error))
            with self.assertRaises(CommunityError) as caught:
                api.verify()
            self.assertIn(expected,str(caught.exception))
            self.assertNotIn('private-key',str(caught.exception))

    def test_public_movie_lists_sorted_by_likes_deduplicated_and_no_show_or_adult(self):
        sources = [{'id':i,'name':'Thrillers '+str(i),'mediatype':'movie','likes':likes,
                    'user_name':'alice','slug':'thrillers-'+str(i)} for i,likes in ((1,2),(2,10),(3,5),(4,1))]
        sources += [{'id':9,'name':'Thriller TV','mediatype':'show','likes':999}]
        calls = []
        def fetch(url):
            calls.append(url)
            path = urllib.parse.urlsplit(url).path
            if path.endswith('/search'):
                return sources
            return {'movies':[{'id':100,'title':'Movie','release_year':2020,'mediatype':'movie'},
                              {'id':101,'title':'Adult','adult':1},
                              {'id':102,'title':'A TV show','mediatype':'show'},
                              {'id':103,'title':'Future','release_year':2999}]}
        picks = MDBList('secret-test-key',fetch=fetch).picks('thriller','Thriller')
        self.assertEqual(len(calls),4)
        self.assertEqual([urllib.parse.urlsplit(url).path for url in calls[1:]],
                         ['/lists/2/items','/lists/3/items','/lists/1/items'])
        self.assertEqual([m['tmdbId'] for m in picks],[100])
        self.assertEqual(picks[0]['community_list'],'Thrillers 2')
        self.assertEqual(picks[0]['community_url'],'https://mdblist.com/lists/alice/thrillers-2')
        self.assertNotIn('secret-test-key',json.dumps(picks))

    def test_errors_hide_tokens_and_partial_lists_still_work(self):
        api = MDBList('private-key',fetch=Mock(side_effect=RuntimeError('URL?apikey=private-key')))
        with self.assertRaises(CommunityError) as error:
            api.verify()
        self.assertNotIn('private-key',str(error.exception))
        responses = [[{'id':1,'mediatype':'movie'}, {'id':2,'mediatype':'movie'}],
                     {'error':'limit'}, {'movies':[{'id':42,'title':'Good'}]}]
        picks = MDBList('key',fetch=Mock(side_effect=responses)).picks('horror','Horror')
        self.assertEqual(picks[0]['tmdbId'],42)

    def test_theme_query_and_empty_results_are_not_an_imdb_fallback(self):
        fetch = Mock(return_value=[])
        self.assertEqual(MDBList('key',fetch=fetch).picks('psychological','Psychological Thrillers'),[])
        self.assertIn('query=Psychological+Thriller',fetch.call_args.args[0])
        with self.assertRaises(CommunityError):
            MDBList('key',fetch=Mock(return_value={'unexpected':True})).verify()


class CommunityBotTests(unittest.TestCase):
    setUp = test_menu.MenuTests.setUp
    tearDown = test_menu.MenuTests.tearDown
    current = test_menu.MenuTests.current

    def test_community_cache_and_library_exclusions(self):
        self.bot.c['MDBLIST_API_KEY']='secret-test-key'
        picks = [dict(title='Owned',tmdbId=99,community=True),dict(title='New',tmdbId=100,community=True)]
        with patch('community.MDBList.picks',return_value=picks) as fetch:
            for _ in range(2):
                result = self.bot.community_picks('thriller',[dict(self.movie,hasFile=True)],set())
                self.assertEqual([m['tmdbId'] for m in result],[100])
        fetch.assert_called_once()
        self.assertNotIn('secret-test-key',self.bot.path.read_text())

    def test_selected_source_never_silently_uses_imdb_on_failure(self):
        self.bot.c['MDBLIST_API_KEY']='key'
        self.bot.rad=Mock(side_effect=[[],{'records':[]}])
        self.bot.catalogue_picks=Mock()
        with patch('community.MDBList.picks',side_effect=CommunityError('Unavailable')):
            self.bot.random_movies('thriller')
        self.bot.catalogue_picks.assert_not_called()
        self.assertIn('MDBList recommendations are still selected',self.current()['text'])

    def test_movie_identity_hydration_and_provenance(self):
        movie = {'title':'Source title','tmdbId':99,'imdbId':'tt1234567','community':True,
                 'community_list':'Best Thrillers','community_genre':'thriller'}
        self.bot.rad=Mock(return_value=[dict(self.movie,tmdbId=999),
                        dict(self.movie,imdbId='tt1234567',genres=['Thriller'])])
        result = self.bot.hydrate_pick(movie)
        self.assertTrue(result['hydrated'])
        self.assertFalse(result['community_mismatch'])
        self.assertEqual(result['community_list'],'Best Thrillers')
        self.bot.rad.return_value=[dict(self.movie,imdbId='tt7654321')]
        wrong = self.bot.hydrate_pick(dict(movie,hydrated=False))
        self.assertFalse(wrong.get('hydrated'))

    def test_wrong_genre_is_not_probed_for_a_download(self):
        self.bot.session('recommend',genre='thriller',index=0,deck=[{
            'title':'Not a thriller','tmdbId':100,'community':True,'hydrated':True,'community_mismatch':True}])
        self.bot.search_releases=Mock()
        self.bot.show_random_pick()
        self.bot.search_releases.assert_not_called()
        self.assertFalse(self.bot.state['session']['verified_pick'])

    def test_community_recommendation_keeps_eligible_download_check_and_source(self):
        self.bot.c['MDBLIST_API_KEY']='key'
        self.bot.rad=Mock(side_effect=[[],{'records':[]}])
        pick=dict(self.movie,community=True,hydrated=True,community_list='Best Thrillers',
                  community_url='https://mdblist.com/lists/alice/thrillers',overview='Plot')
        self.bot.community_picks=Mock(return_value=[pick])
        self.bot.search_releases=Mock(return_value=True)
        self.bot.random_movies('thriller')
        self.bot.search_releases.assert_called_once_with(pick,probe=True)
        self.assertIn('MDBList community list: Best Thrillers',self.current()['text'])
        self.assertIn('https://mdblist.com/lists/alice/thrillers',self.current()['text'])


class CommunitySetupTests(unittest.TestCase):
    def test_key_verified_before_private_save_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg={'home':tmp}
            path=Path(tmp)/'.config/media-request-bot/recommendations.json'
            with patch.object(manage,'prompt',side_effect=['1','secret-test-key']), \
                 patch('community.MDBList.verify') as verify, patch.object(manage,'own'), \
                 patch.object(manage,'run') as run:
                manage.recommendations(cfg)
            verify.assert_called_once()
            self.assertEqual(json.loads(path.read_text())['api_key'],'secret-test-key')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            run.assert_called_once_with(['systemctl','restart','media-request-bot.service'])
            original=path.read_text()
            with patch.object(manage,'prompt',side_effect=['1','different-key']), \
                 patch('community.MDBList.verify',side_effect=CommunityError('Rejected')), \
                 patch.object(manage,'run') as run:
                with self.assertRaises(ValueError):
                    manage.recommendations(cfg)
            self.assertEqual(path.read_text(),original)
            run.assert_not_called()

    def test_switch_back_removes_key_from_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(manage,'prompt',return_value='2'), patch.object(manage,'own'), patch.object(manage,'run'):
                manage.recommendations({'home':tmp})
            data=json.loads((Path(tmp)/'.config/media-request-bot/recommendations.json').read_text())
            self.assertEqual(data,{'source':'imdb'})
