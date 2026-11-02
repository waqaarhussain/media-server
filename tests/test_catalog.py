import sqlite3
import os
import time
import unittest
from unittest.mock import Mock, patch
import test_menu
from catalog import build


class CatalogueTests(unittest.TestCase):
    setUp=test_menu.MenuTests.setUp
    tearDown=test_menu.MenuTests.tearDown
    click=test_menu.MenuTests.click
    message=test_menu.MenuTests.message
    current=test_menu.MenuTests.current

    def catalogue(self):
        ratings=[{'tconst':'tt1111111','averageRating':'8.2','numVotes':'500000'},
                 {'tconst':'tt2222222','averageRating':'6.8','numVotes':'20000'},
                 {'tconst':'tt3333333','averageRating':'9.9','numVotes':'100'},
                 {'tconst':'tt4444444','averageRating':'9','numVotes':'500000'},
                 {'tconst':'tt5555555','averageRating':'8','numVotes':'500000'}]
        basics=[{'tconst':iid,'titleType':kind,'primaryTitle':title,'originalTitle':title,
                 'isAdult':adult,'startYear':'2010','runtimeMinutes':'120','genres':genres}
                for iid,kind,title,adult,genres in (
                    ('tt1111111','movie','Mystery Example','0','Mystery,Thriller'),
                    ('tt2222222','movie','Shutter Island','0','Mystery,Thriller'),
                    ('tt3333333','movie','Low vote sample','0','Mystery'),
                    ('tt4444444','tvEpisode','Not a movie','0','Mystery'),
                    ('tt5555555','movie','Adult excluded','1','Mystery'))]
        with patch('builtins.print'):
            build(self.bot.directory,ratings,basics)

    def test_builder_excludes_episodes_adult_and_tiny_vote_samples(self):
        self.catalogue()
        with sqlite3.connect(self.bot.directory/'catalog.sqlite3') as db:
            titles=[r[0] for r in db.execute('SELECT title FROM movies ORDER BY title')]
        self.assertEqual(titles,['Mystery Example','Shutter Island'])

    def test_daily_refresh_reuses_fresh_catalogue_and_preserves_it_on_network_failure(self):
        self.catalogue()
        path=self.bot.directory/'catalog.sqlite3'
        before=path.read_bytes()
        with patch('catalog.records',side_effect=OSError('offline')) as fetch,patch('builtins.print'):
            build(self.bot.directory)
            fetch.assert_not_called()
            old=time.time()-24*3600
            os.utime(path,(old,old))
            with self.assertRaises(OSError):
                build(self.bot.directory)
            fetch.assert_called_once_with('title.ratings')
        self.assertEqual(path.read_bytes(),before)

    def test_mystery_comes_from_broad_catalogue_without_discovery_feed(self):
        self.catalogue()
        picks=self.bot.catalogue_picks('mystery',[],set())
        self.assertEqual(len(picks),2)
        self.bot.rad.assert_not_called()

    def test_owned_imdb_identity_is_excluded_in_catalogue(self):
        self.catalogue()
        library=[dict(self.movie,imdbId='tt1111111',hasFile=True)]
        picks=self.bot.catalogue_picks('mystery',library,set())
        self.assertEqual([m['title'] for m in picks],['Shutter Island'])

    def test_nullable_imdb_identity_does_not_break_catalogue(self):
        self.catalogue()
        self.assertEqual(len(self.bot.catalogue_picks('mystery',[dict(self.movie,imdbId=None,hasFile=True)],set())),2)

    def test_psychological_catalogue_is_curated_theme(self):
        self.catalogue()
        picks=self.bot.catalogue_picks('psychological',[],set())
        self.assertEqual([m['title'] for m in picks],['Shutter Island'])

    def test_imdb_hydration_matches_exact_id_preserves_dataset_rating_and_synopsis(self):
        self.catalogue()
        movie=self.bot.catalogue_picks('mystery',[],set())[0]
        self.bot.rad.side_effect=None
        self.bot.rad.return_value=[{'title':'Wrong','imdbId':'tt9999999','tmdbId':999},
            {'title':movie['title'],'year':2010,'imdbId':movie['imdbId'],'tmdbId':345,
             'overview':'A detective investigates a disappearance.',
             'remotePoster':'https://example.org/poster.jpg','ratings':None}]
        full=self.bot.hydrate_pick(movie)
        self.assertEqual(full['tmdbId'],345)
        self.assertIn('detective',full['overview'])
        self.assertEqual(full['ratings']['imdb']['value'],8.2)

    def test_recommendation_displays_actual_description_and_poster(self):
        self.catalogue()
        self.bot.hydrate_pick=Mock(side_effect=lambda m:dict(m,tmdbId=345,
            overview='A detective investigates a disappearance.',remotePoster='https://example.org/a.jpg'))
        self.click('global:genre:mystery')
        screen=self.current()
        content=screen.get('caption') or screen.get('media',{}).get('caption') or screen.get('text','')
        self.assertIn('About the movie',content)
        self.assertIn('A detective investigates a disappearance.',content)
        self.assertEqual(self.events,[])

    def test_menu_and_search_prompt_have_no_stale_poster(self):
        self.message('/start')
        self.bot.panel('Movie card',movie={'remotePoster':'https://example.org/a.jpg'})
        self.assertTrue(self.bot.state['ui']['photo'])
        self.click('global:home')
        self.assertFalse(self.bot.state['ui']['photo'])
        self.bot.panel('Movie card',movie={'remotePoster':'https://example.org/a.jpg'})
        self.click('global:search')
        self.assertFalse(self.bot.state['ui']['photo'])
        self.assertEqual(len(self.telegram.messages),1)

    def test_library_has_only_name_and_year_no_movie_actions_or_import_guidance(self):
        self.movie['hasFile']=True
        self.bot.show_downloads(library=True)
        screen=self.current()
        self.assertIn(self.movie['title']+' (2003)',screen['text'])
        for unwanted in ('global:item:','global:delete:','Imported into','Refresh Infuse'):
            self.assertNotIn(unwanted,str(screen))

    def test_full_long_search_title_is_selectable_without_number(self):
        long='A deliberately long movie title '+('with another long subtitle '*6)
        movie=dict(self.movie,title=long)
        self.bot.rad.side_effect=None
        self.bot.rad.return_value=[movie]
        self.bot.find_movies('long movie')
        buttons=self.current()['reply_markup']['inline_keyboard']
        self.assertEqual(buttons[0][0]['text'],long+' (2003)')

    def test_search_pages_keep_selection_identity_and_five_titles(self):
        movies=[dict(self.movie,title=f'Movie {i}',tmdbId=100+i) for i in range(12)]
        self.bot.session('movie',movies=movies)
        self.bot.show_search_results()
        rows=self.current()['reply_markup']['inline_keyboard']
        self.assertEqual([r[0]['text'] for r in rows[:5]],[f'Movie {i} (2003)' for i in range(5)])
        self.bot.show_search_results(1)
        rows=self.current()['reply_markup']['inline_keyboard']
        self.assertEqual(rows[0][0]['text'],'Movie 5 (2003)')
        self.assertTrue(rows[0][0]['callback_data'].endswith(':movie:5'))
        self.assertIn('Previous',[r[0]['text'] for r in rows])
        self.assertIn('Next',[r[0]['text'] for r in rows])
        self.bot.show_search_results(2)
        rows=self.current()['reply_markup']['inline_keyboard']
        self.assertEqual(rows[0][0]['text'],'Movie 10 (2003)')
        self.assertNotIn('Next',[r[0]['text'] for r in rows])


if __name__=='__main__':
    unittest.main()
