import unittest
from unittest.mock import Mock, patch
import test_menu
from bot import Bot, ServiceError, merge_movie_metadata, poster_url, release_search_summary, ranked_movies
from test_bot import release


class SearchDisplayTests(unittest.TestCase):
    setUp = test_menu.MenuTests.setUp
    tearDown = test_menu.MenuTests.tearDown
    current = test_menu.MenuTests.current

    def test_collection_results_are_oldest_first_and_keep_correct_selection_ids(self):
        movies = [dict(self.movie, tmdbId=100+i, title=title, year=year,
                       collection={'tmdbId':228446,'title':'Insidious Collection'})
                  for i, (title, year) in enumerate([
                      ('Insidious: The Red Door', 2023), ('Insidious: Chapter 2', 2013),
                      ('Insidious', 2010), ('Insidious', 2008), ('Insidious: Chapter 3', 2015),
                      ('Insidious: The Last Key', 2018)])]
        self.bot.rad = Mock(return_value=movies)
        self.bot.find_movies('Insidious')
        selected = self.bot.state['session']['movies']
        self.assertEqual([m['year'] for m in selected], [2008, 2010, 2013, 2015, 2018, 2023])
        buttons = self.current()['reply_markup']['inline_keyboard']
        self.assertEqual(buttons[1][0]['text'], '2. Insidious (2010)')
        self.assertTrue(buttons[1][0]['callback_data'].endswith(':movie:1'))
        self.assertEqual(sum(':movie:' in row[0]['callback_data'] for row in buttons), 5)

    def test_missing_managed_poster_preserves_selected_lookup_art_and_description(self):
        self.bot.state['jobs'] = {}
        candidate = dict(self.movie, remotePoster='https://example.org/insidious.jpg', overview='A family investigates a haunting.')
        managed = dict(self.movie, qualityProfileId=7, path=self.bot.c['RADARR_MOVIES_ROOT']+'/Example')

        def api(endpoint, *args, **kwargs):
            if endpoint.startswith('movie?'):
                return [managed]
            if endpoint.startswith('queue?'):
                return {'records': []}
            if endpoint.startswith('release?'):
                return []
            raise AssertionError('Unexpected mutation or request: ' + endpoint)

        self.bot.rad = Mock(side_effect=api)
        self.bot.search_releases(candidate)
        self.assertTrue(self.bot.state['ui']['photo'])
        self.assertEqual(self.bot.state['ui']['poster'], candidate['remotePoster'])
        screen = self.current()
        text = screen.get('caption') or screen.get('media', {}).get('caption')
        self.assertIn('Movie found.', text)
        self.assertIn('configured indexers returned no', text)
        self.assertIn(candidate['overview'], text)
        self.assertEqual(self.bot.state['session']['stage'], 'watch_offer')

    def test_metadata_retry_never_borrows_a_different_movie_poster(self):
        self.bot.rad = Mock(return_value=[dict(self.movie, tmdbId=999, remotePoster='https://example.org/wrong.jpg')])
        found = self.bot.selected_movie_metadata(self.movie)
        self.assertIsNone(poster_url(found))
        self.assertEqual(found['tmdbId'], self.movie['tmdbId'])
        self.bot.rad.return_value = [dict(self.movie, remotePoster='https://example.org/right.jpg')]
        self.assertEqual(poster_url(self.bot.selected_movie_metadata(self.movie)), 'https://example.org/right.jpg')

    def test_missing_art_is_explicit_and_never_leaves_previous_poster(self):
        self.bot.panel('Other movie', movie=dict(self.movie, remotePoster='https://example.org/other.jpg'))
        self.bot.panel('Selected movie', movie=self.movie)
        self.assertFalse(self.bot.state['ui']['photo'])
        self.assertIn('Poster unavailable for this title.', self.current()['text'])

    def test_lookup_image_url_is_used_when_remote_poster_is_missing(self):
        movie = dict(self.movie, images=[{'coverType': 'poster', 'url': 'http://image.tmdb.org/t/p/w500/right.jpg'}])
        self.assertEqual(poster_url(movie), 'https://image.tmdb.org/t/p/w500/right.jpg')
        different = dict(movie, tmdbId=999)
        self.assertIsNone(poster_url(merge_movie_metadata(self.movie, different)))

    def test_release_reasons_explain_filters_without_leaking_raw_indexer_errors(self):
        reasons = release_search_summary([release(size=30*1024**3, approved=False, seeders=0,
                            rejections=['Private tracker URL with secret token'])])
        self.assertIn('1 outside the size limit', reasons)
        self.assertIn('1 no reported seeders', reasons)
        self.assertIn('1 not approved by Radarr', reasons)
        self.assertNotIn('secret', reasons)

    def test_exact_holes_ranks_before_old_partial_titles_and_popular_remake_first(self):
        movies = [dict(self.movie, tmdbId=100+i, title=title, year=year,
                       ratings={'imdb':{'value':7,'votes':votes}})
                  for i, (title, year, votes) in enumerate([
                      ('Black Holes',1970,500000), ('Holes',1990,100), ('Holes',2003,200000)])]
        self.assertEqual([m['year'] for m in ranked_movies('holes',movies)], [2003,1990,1970])
        self.assertEqual(ranked_movies('holes 1990',movies)[0]['year'], 1990)

    def test_verified_collection_excludes_title_matches_and_orders_members(self):
        movies = [dict(self.movie, tmdbId=100+i, title=title, year=year,
                       collection=collection)
                  for i, (title, year, collection) in enumerate([
                      ('Avengers: Endgame',2019,{'tmdbId':86311,'title':'The Avengers Collection'}),
                      ('The Avengers',2012,{'tmdbId':86311,'title':'The Avengers Collection'}),
                      ('The New Avengers documentary',1980,None),
                      ('Avengers: Age of Ultron',2015,{'tmdbId':86311,'title':'The Avengers Collection'})])]
        ordered = ranked_movies('Avengers',movies)
        self.assertEqual([m['year'] for m in ordered], [2012,2015,2019])

    def test_harry_potter_uses_metadata_ids_not_shared_title_words(self):
        years = [2011,2001,2002,2004,2005,2007,2009,2010]
        movies = [dict(self.movie,tmdbId=100+i,title=f'Harry Potter film {i}',year=year,
                       collection={'title':'Harry Potter Collection','tmdbId':1241})
                  for i,year in enumerate(years)]
        movies += [dict(self.movie,tmdbId=800,title='Harry Potter: Witchcraft Repackaged',year=2001),
                   dict(self.movie,tmdbId=801,title='Harry Potter und ein Stein',year=2006,
                        collection={'title':'Fan parody Collection','tmdbId':999}),
                   dict(self.movie,tmdbId=802,title='Harry Potter: A History of Magic',year=2017)]
        for query in ('Harry Potter','Harry Potter collection','Harry Potter box set'):
            ordered = ranked_movies(query,movies)
            self.assertEqual([m['year'] for m in ordered], sorted(years))
            self.assertEqual({m['tmdbId'] for m in ordered},set(range(100,108)))
        # Explicit searches for a documentary remain possible.
        self.assertEqual(ranked_movies('Harry Potter: A History of Magic',movies)[0]['tmdbId'],802)

    def test_missing_collection_metadata_never_invents_a_collection_from_prefixes(self):
        movies = [dict(self.movie,tmdbId=100,title='Example documentary',year=1970),
                  dict(self.movie,tmdbId=101,title='Example',year=2000)]
        self.assertEqual(ranked_movies('Example',movies)[0]['tmdbId'],101)

    def test_search_cover_strip_uses_exact_page_and_preserves_selection_indices(self):
        movies = [dict(self.movie,tmdbId=100+i,remotePoster=f'https://example.org/{i}.jpg') for i in range(7)]
        self.bot.session('movie',movies=movies)
        with patch('search_art.poster_strip',return_value=b'cover-jpeg') as art:
            self.bot.show_search_results(1)
        art.assert_called_once_with(['https://example.org/5.jpg','https://example.org/6.jpg'],first=6)
        self.assertTrue(self.bot.state['ui']['photo'])
        self.assertTrue(self.current()['reply_markup']['inline_keyboard'][0][0]['callback_data'].endswith(':movie:5'))

    def test_tv_placeholder_accepts_text_without_movie_lookup(self):
        message = {'from':{'id':1},'chat':{'id':1,'type':'private'}}
        self.bot.handle({'callback_query':{'id':'tv','from':{'id':1},'message':message,'data':'global:tvsearch'}})
        self.bot.rad.reset_mock()
        self.bot.handle({'message':dict(message,text='Breaking Bad')})
        self.assertIn('Breaking Bad',self.current()['text'])
        self.bot.rad.assert_not_called()
        self.assertEqual([b['callback_data'] for b in Bot.home_controls()['inline_keyboard'][0]],
                         ['global:search','global:tvsearch'])

    def test_contact_sheet_upload_uses_multipart_without_persisting_image_bytes(self):
        self.bot.c['TELEGRAM_BOT_TOKEN'] = 'test-token'
        with patch('bot.request',return_value={'ok':True,'result':{}}) as api:
            Bot.tg(self.bot,'editMessageMedia',{'chat_id':1,'message_id':2,
                   'media':{'type':'photo','media':'attach://cover'},'_cover':b'test-cover'})
        self.assertIsInstance(api.call_args.args[1],bytes)
        self.assertIn(b'name="cover"; filename="covers.jpg"',api.call_args.args[1])
        self.assertIn(b'test-cover',api.call_args.args[1])
        self.assertIn('multipart/form-data',api.call_args.kwargs['headers']['Content-Type'])

    def test_availability_probe_requires_eligible_release_and_never_grabs(self):
        self.bot.state['jobs'].clear()
        candidate = dict(self.movie,qualityProfileId=7,path=self.bot.c['RADARR_MOVIES_ROOT']+'/Example')
        for returned, expected in [([release(seeders=0)],False),([release(rejected=True)],False),([release()],True)]:
            self.bot.rad = Mock(side_effect=[[candidate],{'records':[]},returned])
            self.assertEqual(self.bot.search_releases(dict(candidate),probe=True),expected)
            self.assertFalse(any(c.args[0]=='release' or c.kwargs.get('method') for c in self.bot.rad.call_args_list))

    def test_random_skips_unavailable_and_never_shows_unchecked_selection(self):
        deck = [dict(self.movie,tmdbId=100+i,title=f'Film {i}') for i in range(2)]
        self.bot.session('recommend',genre='thriller',deck=deck,index=0)
        self.bot.search_releases = Mock(side_effect=[False,True])
        self.bot.show_random_pick()
        self.assertEqual(self.bot.state['session']['index'],1)
        self.assertTrue(self.bot.state['session']['verified_pick'])
        self.assertIn('Film 1',str(self.current()))
        self.assertEqual(self.bot.search_releases.call_count,2)

    def test_random_availability_failure_offers_retry_without_download(self):
        self.bot.session('recommend',genre='thriller',deck=[dict(self.movie)],index=0)
        self.bot.search_releases = Mock(side_effect=ServiceError('offline'))
        self.bot.show_random_pick()
        self.assertFalse(self.bot.state['session']['verified_pick'])
        self.assertNotIn('recselect',str(self.current()))
        self.assertIn('reccheck',str(self.current()))


if __name__ == '__main__':
    unittest.main()
