import unittest
from unittest.mock import Mock
import test_menu
from bot import merge_movie_metadata, poster_url, release_search_summary
from test_bot import release


class SearchDisplayTests(unittest.TestCase):
    setUp = test_menu.MenuTests.setUp
    tearDown = test_menu.MenuTests.tearDown
    current = test_menu.MenuTests.current

    def test_search_results_are_oldest_first_and_keep_correct_selection_ids(self):
        movies = [dict(self.movie, tmdbId=100+i, title=title, year=year)
                  for i, (title, year) in enumerate([
                      ('Insidious: The Red Door', 2023), ('Insidious: Chapter 2', 2013),
                      ('Insidious', 2010), ('Insidious', 2008), ('Insidious: Chapter 3', 2015),
                      ('Insidious: The Last Key', 2018)])]
        self.bot.rad = Mock(return_value=movies)
        self.bot.find_movies('Insidious')
        selected = self.bot.state['session']['movies']
        self.assertEqual([m['year'] for m in selected], [2008, 2010, 2013, 2015, 2018, 2023])
        buttons = self.current()['reply_markup']['inline_keyboard']
        self.assertEqual(buttons[1][0]['text'], 'Insidious (2010)')
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


if __name__ == '__main__':
    unittest.main()
