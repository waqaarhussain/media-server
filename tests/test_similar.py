import copy
import datetime
import time
import unittest
import urllib.parse
from unittest.mock import Mock,patch

import test_menu
from bot import ServiceError
import similar


def movie(tid,title,**extra):
    return dict({'tmdbId':tid,'title':title,'year':2010,'genres':['Science Fiction','Thriller'],
                 'keywords':['dream','subconscious'],'overview':'A thief enters a shared dream to steal secrets.',
                 'remotePoster':'https://example.org/'+str(tid)+'.jpg',
                 'ratings':{'imdb':{'value':8,'votes':100000}}},**extra)


class RankingTests(unittest.TestCase):
    def test_plot_and_theme_match_beats_generic_genre_match(self):
        seed=movie(1,'Seed')
        generic=movie(2,'Other thriller',keywords=['alien invasion'],overview='Soldiers defend Earth against invaders.')
        thematic=movie(3,'Shared dream',keywords=['dream','subconscious'],overview='A team enters a shared dream to steal secrets.')
        result=similar.rank(seed,[generic,thematic])
        self.assertEqual([m['tmdbId'] for m in result],[3,2])
        self.assertTrue(any('Shared themes' in r for r in result[0]['similar_reasons']))

    def test_wrong_seed_duplicates_self_and_invalid_ids_are_rejected(self):
        data={'TmdbId':1,'Recommendations':[{'TmdbId':1},{'TmdbId':2},{'TmdbId':2},
                                             {'TmdbId':'3'},{'TmdbId':True},{'TmdbId':-1}]}
        self.assertEqual(similar.related_ids(data,1),[2])
        self.assertEqual(similar.related_ids(data,999),[])
        self.assertEqual([m['tmdbId'] for m in similar.rank(movie(1,'Seed'),[movie(1,'Seed'),movie(2,'Match'),movie(2,'Duplicate')])],[2])

    def test_generic_adaptation_and_credit_tags_are_not_presented_as_story_themes(self):
        result=similar.rank(movie(1,'Seed',keywords=['based on novel or book']),
                            [movie(2,'Match',keywords=['based on novel or book'])])
        self.assertFalse(any('Shared themes' in r for r in result[0]['similar_reasons']))

    def test_sparse_metadata_does_not_invent_shared_themes(self):
        result=similar.rank(movie(1,'Seed',genres=[],keywords=[],overview=''),
                            [movie(2,'Match',genres=[],keywords=[],overview='')])
        self.assertEqual(result[0]['similar_reasons'],['Related-film suggestion from Radarr metadata'])


class SimilarFlowTests(unittest.TestCase):
    tearDown=test_menu.MenuTests.tearDown
    message=test_menu.MenuTests.message
    click=test_menu.MenuTests.click
    current=test_menu.MenuTests.current

    def setUp(self):
        test_menu.MenuTests.setUp(self)
        self.seed=movie(301,'Inception',year=2010)
        self.alternative=movie(302,'Another Inception',year=1999)
        self.matches={401:movie(401,'Dream Match'),402:movie(402,'Another Dream Match')}
        self.library=[]
        def rad(endpoint,*args,**kwargs):
            if endpoint=='movie':return copy.deepcopy(self.library)
            if endpoint.startswith('movie/lookup?'):
                term=urllib.parse.parse_qs(urllib.parse.urlsplit(endpoint).query)['term'][0]
                if term.startswith('tmdb:'):
                    candidate=self.matches.get(int(term.split(':')[1]))
                    return [copy.deepcopy(candidate)] if candidate else []
                return copy.deepcopy([self.seed,self.alternative])
            raise AssertionError('Unexpected mutation or API call: '+endpoint)
        self.bot.rad=Mock(side_effect=rad)
        self.bot.full_queue=Mock(return_value=[])
        self.bot.search_releases=Mock(return_value=True)
        self.metadata=patch('bot.request',return_value={'TmdbId':301,'Recommendations':[{'TmdbId':401},{'TmdbId':402}]})
        self.source=self.metadata.start();self.addCleanup(self.metadata.stop)
        self.art=patch('search_art.poster_strip',return_value=None);self.art.start();self.addCleanup(self.art.stop)

    def action(self,value):
        self.click(self.bot.state['session']['nonce']+':'+value)

    def text(self):
        screen=self.current()
        return screen.get('text') or screen.get('caption') or screen.get('media',{}).get('caption','')

    def confirm(self):
        self.click('global:similar');self.message('Inception')
        self.action('similaryes')

    def test_menu_places_random_left_and_similar_right_on_same_row(self):
        self.message('/start')
        row=self.current()['reply_markup']['inline_keyboard'][1]
        self.assertEqual([b['callback_data'] for b in row],['global:genres','global:similar'])
        self.assertIn('Find Similar Movie',row[1]['text'])

    def test_search_requires_correct_movie_confirmation_with_poster_before_mode_choice(self):
        self.click('global:similar');self.message('Inception')
        self.assertEqual(self.bot.state['session']['stage'],'similar_confirm')
        self.assertIn('Is this the correct movie?',self.text())
        self.assertTrue(self.bot.state['ui']['photo'])
        self.assertEqual(self.bot.state['ui']['poster'],self.seed['remotePoster'])
        self.source.assert_not_called();self.bot.search_releases.assert_not_called()
        self.action('similaryes')
        self.assertEqual(self.bot.state['session']['stage'],'similar_mode')
        self.assertIn('Recommendations only',self.text())
        self.source.assert_not_called()

    def test_no_returns_to_title_choices_and_another_selection_needs_confirmation(self):
        self.click('global:similar');self.message('Inception');self.action('similarno')
        self.assertEqual(self.bot.state['session']['purpose'],'similar')
        self.action('movie:1')
        self.assertEqual(self.bot.state['session']['stage'],'similar_confirm')
        self.assertIn('Another Inception',self.text())
        self.bot.search_releases.assert_not_called();self.source.assert_not_called()

    def test_recommendations_only_skips_download_checks_and_hides_download_actions(self):
        self.confirm();self.action('similarmode:any')
        self.assertEqual(self.bot.state['session']['mode'],'similar')
        self.assertFalse(self.bot.state['session']['downloads_only'])
        self.bot.search_releases.assert_not_called();self.bot.full_queue.assert_not_called()
        self.assertIn('Why this matches',self.text())
        self.assertIn('availability not checked',self.text())
        self.assertNotIn('recselect',str(self.current()['reply_markup']))
        self.assertNotIn('release',str(self.bot.rad.call_args_list))
        self.action('recnext');self.action('recprev');self.action('recselect')
        self.bot.search_releases.assert_not_called()
        self.assertEqual(len(self.telegram.messages),1)

    def test_download_mode_checks_eligibility_and_keeps_download_explicit(self):
        self.confirm();self.action('similarmode:downloads')
        self.assertTrue(self.bot.state['session']['verified_pick'])
        self.assertTrue(all(call.kwargs.get('probe') is True for call in self.bot.search_releases.call_args_list))
        self.assertIn('recselect',str(self.current()['reply_markup']))
        self.bot.search_releases.reset_mock()
        selected=self.bot.state['session']['deck'][0]
        self.action('recselect')
        self.bot.search_releases.assert_called_once_with(selected)
        self.assertEqual(self.bot.state['recommend_return']['mode'],'similar')

    def test_unavailable_releases_are_not_shown_in_download_mode_but_are_in_recommendations_only(self):
        self.bot.search_releases.return_value=False
        self.confirm();self.action('similarmode:downloads')
        self.assertIn('No eligible download',self.text())
        self.assertNotIn('recselect',str(self.current()['reply_markup']))
        self.action('recgenres');self.message('Inception');self.action('similaryes');self.action('similarmode:any')
        self.assertIn('Dream Match',self.text())
        self.assertNotIn('No eligible download',self.text())

    def test_change_mode_returns_to_same_seed_and_preserves_the_movie(self):
        self.confirm();self.action('similarmode:any');self.action('recmode')
        self.assertEqual(self.bot.state['session']['movie']['tmdbId'],301)
        self.assertEqual(self.bot.state['session']['stage'],'similar_mode')
        self.action('similarseed')
        self.assertEqual(self.bot.state['session']['stage'],'similar_confirm')

    def test_future_seed_self_and_downloaded_movies_are_not_download_suggestions(self):
        self.matches[401]['hasFile']=True
        self.matches[402]['year']=datetime.date.today().year+1
        self.confirm();self.action('similarmode:downloads')
        self.assertIn('No other released movies',self.text())
        self.bot.search_releases.assert_not_called()

    def test_wrong_or_unavailable_provider_data_never_falls_back_to_random_movies(self):
        for response in ({'TmdbId':999,'Recommendations':[{'TmdbId':401}]},{}):
            self.source.return_value=response
            self.confirm();self.action('similarmode:any')
            self.assertIn('No related-film suggestions',self.text())
        self.source.side_effect=ServiceError('offline')
        self.confirm();self.action('similarmode:any')
        self.assertIn('could not be loaded',self.text())
        self.bot.search_releases.assert_not_called()

    def test_old_confirmation_cannot_approve_a_different_seed(self):
        self.click('global:similar');self.message('Inception')
        old=self.bot.state['session']['nonce']+':similaryes'
        self.action('similarno');self.action('movie:1')
        self.click(old)
        self.assertEqual(self.bot.state['session']['movie']['tmdbId'],302)
        self.source.assert_not_called()

    def test_download_card_can_return_to_the_same_seed_and_recommendation_mode(self):
        self.confirm();self.action('similarmode:downloads')
        self.action('recselect')
        selected=self.bot.state['recommend_return']['deck'][0]
        self.bot.show_recommendation(selected,[test_menu.release()],True)
        self.assertIn('recback',str(self.current()['reply_markup']))
        self.action('recback')
        self.assertEqual(self.bot.state['session']['mode'],'similar')
        self.assertEqual(self.bot.state['session']['seed']['tmdbId'],301)
        self.assertTrue(self.bot.state['session']['downloads_only'])

    def test_normal_movie_search_still_opens_the_download_flow(self):
        self.click('global:search');self.message('Inception')
        self.assertEqual(self.bot.state['session']['stage'],'movie')
        self.action('movie:0')
        self.bot.search_releases.assert_called_once()
        self.source.assert_not_called()


if __name__=='__main__':unittest.main()
