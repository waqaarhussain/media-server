import copy
import time
import unittest
from unittest.mock import Mock, patch
import test_menu
from bot import GENRES, ServiceError, rating, recommended


def pick(tid=300,title='Example thriller',**changes):
    return dict({'tmdbId':tid,'title':title,'year':2010,'genres':['Thriller'],
                 'overview':'A psychological mystery involving paranoia.',
                 'ratings':{'imdb':{'value':8.1,'votes':150000},
                            'rottenTomatoes':{'value':90,'votes':1}},
                 'remotePoster':'https://example.org/poster.jpg'},**changes)


class FeatureTests(unittest.TestCase):
    setUp=test_menu.MenuTests.setUp
    tearDown=test_menu.MenuTests.tearDown
    message=test_menu.MenuTests.message
    click=test_menu.MenuTests.click
    current=test_menu.MenuTests.current

    def feed(self,movies):
        # These feed/navigation tests isolate availability, covered separately.
        self.bot.search_releases=Mock(return_value=True)
        previous=self.bot.rad.side_effect
        self.bot.rad.side_effect=lambda endpoint,*a,**kw: copy.deepcopy(movies) if endpoint.startswith('importlist/movie?') else previous(endpoint,*a,**kw)

    def test_library_is_plain_full_titles_and_years_without_details_buttons(self):
        self.movie['hasFile']=True
        self.bot.show_downloads(library=True)
        screen=self.current()
        self.assertEqual(screen['text'].count(self.movie['title']),1)
        self.assertIn('(2003)',screen['text'])
        self.assertNotIn('global:item:',str(screen['reply_markup']))
        self.assertNotIn('global:delete:',str(screen))

    def test_deletion_search_matches_any_part_case_and_accents_without_mutations(self):
        self.movie['title']='Amélie and the Great Journey'
        self.click('global:manage:0')
        self.message('JOURNEY')
        self.assertIn(self.movie['title'],str(self.current()['reply_markup']))
        self.message('amelie')
        self.assertIn(self.movie['title'],str(self.current()['reply_markup']))
        self.assertEqual(self.events,[])

    def test_deletion_search_no_match_never_opens_a_delete_approval(self):
        self.click('global:manage:0')
        self.message('not in the library')
        self.assertIn('No matching movies',self.current()['text'])
        self.assertEqual(self.bot.state['delete_prompts'],{})
        self.assertEqual(self.events,[])

    def test_search_result_opens_exact_details_then_confirms_named_deletion(self):
        self.click('global:manage:0')
        self.message('long title')
        target=self.current()['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.click(target)
        self.assertEqual(self.bot.state['view']['route'],'detail')
        self.assertEqual(self.bot.state['view']['back']['route'],'delete_results')
        token=self.bot.movie_target(self.movie)
        self.click('global:delete:'+token)
        self.assertIn(self.movie['title'],self.current()['text'])
        self.assertEqual(self.events,[])
        nonce=next(iter(self.bot.state['delete_prompts']))
        self.click('global:keep:'+nonce)
        self.click('global:back')
        self.assertEqual(self.bot.state['view']['route'],'delete_results')

    def test_genre_menu_has_full_standard_set_plus_psychological_thrillers(self):
        self.click('global:genres')
        rendered=str(self.current()['reply_markup'])
        for name in GENRES.values():
            self.assertIn(name,rendered)
        self.assertIn('Documentary',rendered)
        self.assertIn('Western',rendered)
        self.assertIn('Psychological Thrillers',rendered)
        self.assertEqual(self.events,[])

    def test_random_next_previous_show_one_pick_and_never_add_to_radarr(self):
        self.feed([pick(),pick(301,'Another thriller'),pick(302,'Third thriller')])
        self.message('/start')
        self.click('global:genre:thriller')
        s=self.bot.state['session']
        first=s['deck'][0]['tmdbId']
        self.bot.callback('recnext')
        self.assertEqual(s['index'],1)
        self.bot.callback('recprev')
        self.assertEqual(s['deck'][s['index']]['tmdbId'],first)
        self.assertEqual(len(self.telegram.messages),1)
        self.assertEqual(self.events,[])
        self.assertFalse(any(c.args[0]=='release' or c.kwargs.get('method') for c in self.bot.rad.call_args_list))

    def test_random_uses_cached_feed_and_excludes_downloaded_or_queued_movies(self):
        self.movie['hasFile']=True
        self.feed([pick(self.movie['tmdbId'],self.movie['title']),pick(301,'Unseen thriller')])
        self.click('global:genre:thriller')
        self.assertEqual([m['tmdbId'] for m in self.bot.state['session']['deck']],[301])
        self.click('global:genres')
        self.click('global:genre:thriller')
        self.assertEqual(sum(c.args[0].startswith('importlist/movie?') for c in self.bot.rad.call_args_list),1)

    def test_missing_rotten_tomatoes_is_labelled_and_not_invented(self):
        candidate=pick(ratings={'imdb':{'value':8.0,'votes':90000}})
        self.feed([candidate])
        self.click('global:genre:thriller')
        screen=self.current()
        self.assertIn('Rotten Tomatoes: Not available',screen.get('caption',screen.get('text',screen.get('media',{}).get('caption',''))))

    def test_recommendation_find_download_is_explicit_and_preserves_card_return(self):
        self.feed([pick(),pick(301)])
        self.click('global:genre:thriller')
        self.bot.callback('recnext')
        selected=self.bot.state['session']['deck'][1]
        self.bot.search_releases=Mock()
        self.bot.callback('recselect')
        self.bot.search_releases.assert_called_once_with(selected)
        self.assertEqual(self.bot.state['recommend_return']['index'],1)

    def test_no_qualified_recommendations_does_not_relax_rating_rules(self):
        self.feed([pick(ratings={'imdb':{'value':5,'votes':500000}})])
        self.click('global:genre:thriller')
        self.assertIn('No matching movies are available',self.current()['text'])
        self.assertEqual(self.events,[])

    def test_live_qbit_progress_overrides_radarr_cached_progress(self):
        self.client.torrents.return_value[0].update(progress=.76,dlspeed=2*1024**2,eta=120,state='downloading')
        self.bot.show_downloads()
        self.assertIn('76.0%',str(self.current()['reply_markup']))
        self.assertIn('qBittorrent live',self.current()['text'])
        token=self.bot.movie_target(self.movie)
        self.click('global:item:'+token)
        text=self.current()['text']
        self.assertIn('Speed: 2.0 MiB/s',text)
        self.assertIn('Time left: 2 min',text)
        self.assertEqual(self.events,[])

    def test_live_failure_explicitly_falls_back_to_radarr_snapshot(self):
        self.client.torrents.side_effect=ServiceError('offline')
        self.bot.show_downloads()
        self.assertIn('Radarr queue snapshot',self.current()['text'])
        self.assertIn('50%',str(self.current()['reply_markup']))

    def test_live_stats_do_not_touch_confirmation_or_recommendation_panels(self):
        self.message('/start')
        self.click('global:delete:'+self.bot.movie_target(self.movie))
        self.assertFalse(self.bot.live_view())
        screen=copy.deepcopy(self.current())
        self.bot.refresh_visible()
        self.assertEqual(self.current(),screen)

    def test_live_connection_and_snapshot_are_reused_only_briefly(self):
        with patch('bot.time.monotonic',return_value=100):
            self.bot.live_downloads(self.queue)
            self.bot.live_downloads(self.queue)
        self.assertEqual(self.client.torrents.call_count,1)
        with patch('bot.time.monotonic',return_value=105):
            self.bot.live_downloads(self.queue)
        self.assertEqual(self.client.torrents.call_count,2)


class RecommendationPolicyTests(unittest.TestCase):
    def test_requires_real_high_imdb_score_and_substantial_votes(self):
        self.assertTrue(recommended(pick(),'thriller'))
        self.assertFalse(recommended(pick(ratings={}), 'thriller'))
        self.assertFalse(recommended(pick(ratings={'imdb':{'value':9,'votes':100}}),'thriller'))
        self.assertFalse(recommended(pick(ratings={'imdb':{'value':6.4,'votes':900000}}),'thriller'))

    def test_rt_is_informational_and_not_a_hard_veto(self):
        candidate=pick()
        candidate['ratings']['rottenTomatoes']['value']=50
        self.assertTrue(recommended(candidate,'thriller'))

    def test_psychological_requires_thriller_genre_and_plot_theme(self):
        self.assertTrue(recommended(pick(),'psychological'))
        self.assertFalse(recommended(pick(genres=['Drama']),'psychological'))
        self.assertFalse(recommended(pick(overview='An action-packed car chase.'),'psychological'))

    def test_missing_genres_and_invalid_ratings_fail_closed(self):
        self.assertFalse(recommended(pick(genres=None),'thriller'))
        self.assertEqual(rating(pick(ratings={'imdb':{'value':'NaN','votes':100000}}),'imdb'),(0,0))

    def test_qbit_complete_waits_for_import_not_ready_claim(self):
        from bot import Bot
        self.assertIn('waiting for Radarr import',Bot.qbit_label({'progress':1,'dlspeed':0,'state':'stalledUP'}))


if __name__=='__main__':
    unittest.main()
