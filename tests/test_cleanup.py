import copy
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from bot import Bot, ServiceError
from test_bot import release


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = {'TELEGRAM_ALLOWED_USER_ID':'1','TELEGRAM_CHAT_ID':'1','RADARR_PROFILE_ID':'7',
                       'RADARR_MOVIES_ROOT':str(self.root/'Movies'),
                       'RADARR_KIDS_ROOT':str(self.root/'Kids/Movies')}
        self.bot = Bot(self.config,self.root/'state')
        self.bot.tg = Mock(return_value={})
        self.movie = {'id':42,'tmdbId':99,'title':'Example','path':str(self.root/'Movies/Example'),
                      'qualityProfileId':7,'monitored':False,'minimumAvailability':'released',
                      'added':'2026-11-02T16:00:00Z','tags':[], 'hasFile':False,'movieFileId':0,'sizeOnDisk':0}
        self.bot.rad = Mock()

    def tearDown(self):
        self.tmp.cleanup()

    def own(self):
        self.bot.remember_preview(self.movie)

    def normal_responses(self,movie=None):
        self.bot.rad.side_effect = [{'records':[]},movie or self.movie,{}]

    def no_delete(self):
        self.assertFalse(any(c.kwargs.get('method')=='DELETE' for c in self.bot.rad.call_args_list))

    def test_cancel_deletes_only_owned_metadata(self):
        self.own()
        self.bot.session('versions',movie=self.movie,releases=[release()],uhd=True)
        self.normal_responses()
        self.bot.callback('cancel')
        call=self.bot.rad.call_args_list[-1]
        self.assertEqual(call.args[0],'movie/42?deleteFiles=false&addImportExclusion=false')
        self.assertEqual(call.kwargs,{'method':'DELETE'})
        self.assertEqual(self.bot.state['previews'],{})

    def test_existing_unowned_movie_is_never_removed(self):
        self.bot.session('versions',movie=self.movie)
        self.bot.end_browse()
        self.bot.rad.assert_not_called()

    def test_active_preview_is_kept(self):
        self.own()
        self.bot.session('versions',movie=self.movie)
        self.bot.cleanup_previews()
        self.bot.rad.assert_not_called()
        self.assertIn('42',self.bot.state['previews'])

    def test_expired_preview_is_removed(self):
        self.own()
        self.bot.session('confirm',movie=self.movie)
        self.bot.state['session']['expires']=time.time()-1
        self.normal_responses()
        self.bot.cleanup_previews()
        self.assertIsNone(self.bot.state['session'])
        self.assertEqual(self.bot.rad.call_args.kwargs,{'method':'DELETE'})

    def test_approved_request_is_kept_even_if_response_was_uncertain(self):
        self.own()
        self.bot.state['jobs']['42']={'status':'submitting'}
        self.bot.rad.return_value={'records':[]}
        self.bot.cleanup_previews()
        self.assertEqual(self.bot.rad.call_count,1)
        self.no_delete()

    def test_queued_movie_is_kept(self):
        self.own()
        self.bot.rad.return_value={'records':[{'movieId':42}]}
        self.bot.cleanup_previews()
        self.assertEqual(self.bot.rad.call_count,1)
        self.no_delete()

    def test_imported_movie_is_kept(self):
        self.own()
        self.normal_responses({**self.movie,'hasFile':True,'movieFileId':5})
        self.bot.cleanup_previews()
        self.no_delete()

    def test_user_edits_make_entry_permanent(self):
        for change in ({'monitored':True},{'path':'/another/location'},
                       {'qualityProfileId':8},{'tags':[55]},
                       {'added':'2026-11-02T17:00:00Z'}):
            self.own()
            self.bot.rad.reset_mock()
            self.normal_responses({**self.movie,**change})
            self.bot.cleanup_previews()
            self.no_delete()
            self.assertEqual(self.bot.state['previews'],{})

    def test_queue_failure_retains_ledger_for_retry(self):
        self.own()
        self.bot.rad.side_effect=ServiceError('timeout')
        self.bot.cleanup_previews()
        self.assertIn('42',self.bot.state['previews'])
        self.no_delete()

    def test_unmapped_or_incomplete_queue_prevents_deletion(self):
        for queue in ({'records':[{'movieId':None}]},{'records':[],'totalRecords':1001},{}):
            self.own()
            self.bot.rad.reset_mock()
            self.bot.rad.return_value=queue
            self.bot.cleanup_previews()
            self.no_delete()
            self.assertIn('42',self.bot.state['previews'])

    def test_manually_deleted_preview_drops_ledger(self):
        self.own()
        self.bot.rad.side_effect=[{'records':[]},ServiceError('gone',status=404)]
        self.bot.cleanup_previews()
        self.assertEqual(self.bot.state['previews'],{})

    def test_delete_failure_retried_later(self):
        self.own()
        self.bot.rad.side_effect=[{'records':[]},self.movie,ServiceError('timeout')]
        self.bot.cleanup_previews()
        self.assertIn('42',self.bot.state['previews'])
        self.normal_responses()
        self.bot.cleanup_previews()
        self.assertNotIn('42',self.bot.state['previews'])

    def test_unimported_files_in_folder_are_kept(self):
        folder=Path(self.movie['path'])
        folder.mkdir(parents=True)
        (folder/'movie.mkv').write_bytes(b'example')
        self.own()
        self.normal_responses()
        self.bot.cleanup_previews()
        self.no_delete()
        self.assertTrue((folder/'movie.mkv').exists())

    def test_ownership_survives_restart(self):
        self.own()
        restarted=Bot(self.config,self.root/'state')
        restarted.rad=Mock(side_effect=[{'records':[]},self.movie,{}])
        restarted.cleanup_previews()
        self.assertEqual(restarted.rad.call_args.kwargs,{'method':'DELETE'})

    def test_missing_ownership_timestamp_is_not_adopted(self):
        movie=copy.deepcopy(self.movie)
        movie.pop('added')
        self.bot.remember_preview(movie)
        self.bot.cleanup_previews()
        self.bot.rad.assert_not_called()

    def test_failed_search_cleans_up_new_preview(self):
        self.bot.rad.side_effect=[[],self.movie,{'records':[]},[],{'records':[]},self.movie,{}]
        self.bot.search_releases({'tmdbId':99,'title':'Example'})
        self.assertEqual(self.bot.state['session']['stage'],'watch_offer')
        self.bot.callback('cancel')
        self.assertEqual(self.bot.rad.call_args.kwargs,{'method':'DELETE'})

    def test_download_approval_removes_disposable_ownership(self):
        self.own()
        self.bot.session('confirm',movie=self.movie,release=release(),root=self.config['RADARR_MOVIES_ROOT'])
        self.bot.rad.side_effect=[self.movie,{'records':[]},[release()],{}]
        self.bot.callback('download')
        self.assertEqual(self.bot.state['previews'],{})
        self.assertEqual(self.bot.state['jobs']['42']['status'],'submitted')
        self.bot.cleanup_previews()
        self.no_delete()


if __name__=='__main__':
    unittest.main()
