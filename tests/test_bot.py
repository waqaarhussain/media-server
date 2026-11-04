import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from bot import Bot, choose_releases, eligible, title_score, ServiceError, release_details


def release(quality="WEBDL-2160p", **changes):
    r = {"quality": {"quality": {"name": quality}}, "approved": True,
         "downloadAllowed": True, "rejected": False, "rejections": [],
         "size": 10*1024**3, "protocol": "torrent", "seeders": 10,
         "title": "Example.2008.2160p.WEB-DL.x265.HDR.DDP5.1", "guid": "abc", "indexerId": 1}
    r.update(changes)
    return r


class PolicyTests(unittest.TestCase):
    def test_friendly_audio_hdr_labels(self):
        self.assertEqual(release_details(release(title='Example.2160p.DDP5.1.DV.HDR.H265')),
                         ('4K','WEB-DL','Dolby Vision / HDR','Dolby Digital Plus 5.1'))

    def test_unknown_does_not_invent_audio_or_hdr(self):
        self.assertEqual(release_details(release(title='Example.2160p.H265.10bit'))[2:],
                         ('Unknown','Unknown'))

    def test_channel_count_does_not_claim_surround_layout(self):
        self.assertEqual(release_details(release(title='Example.2160p.6CH'))[3],'6 channels')

    def test_atmos_and_truehd(self):
        self.assertEqual(release_details(release(title='Example.TrueHD.7.1.Atmos.HDR10+'))[2:],
                         ('HDR10+','Dolby TrueHD 7.1 · Atmos'))
    def test_4k_hides_1080_even_if_lower_score(self):
        a, b = release(customFormatScore=0), release("Bluray-1080p", guid="b", customFormatScore=190)
        self.assertEqual(choose_releases([b,a]), ([a], True))

    def test_rejected_4k_allows_1080_fallback(self):
        a,b = release(rejected=True), release("WEBRip-1080p", guid="b")
        self.assertEqual(choose_releases([a,b]), ([b],False))

    def test_rejects_remux_disk_hdtv_720(self):
        for q in ("Remux-2160p", "BR-DISK", "HDTV-2160p", "Bluray-720p"):
            self.assertFalse(eligible(release(q)))

    def test_size_cap_and_boundary(self):
        self.assertTrue(eligible(release(size=25*1024**3)))
        self.assertFalse(eligible(release(size=25*1024**3+1)))
        self.assertFalse(eligible(release(size=0)))

    def test_missing_approval_seeds_and_rejections_fail_closed(self):
        for changes in ({"approved":False}, {"approved":None}, {"downloadAllowed":False},
                        {"seeders":0}, {"temporarilyRejected":True}, {"rejections":["Bad language"]}):
            self.assertFalse(eligible(release(**changes)))

    def test_avoid_manipulated_release_labels(self):
        for title in ("Example.2160p.AI.Upscale", "Example.2160p.60FPS", "Rifftrax Example"):
            self.assertFalse(eligible(release(title=title)))

    def test_dedup_and_score_sort(self):
        a=release(customFormatScore=50,seeders=2)
        b=release(guid="b",customFormatScore=150,seeders=1)
        self.assertEqual(choose_releases([a,b,copy.deepcopy(b)])[0], [b,a])

    def test_fuzzy_ranks_partial_misspelled_title(self):
        self.assertGreater(title_score("hsrry potter chamb", {"title":"Harry Potter and the Chamber of Secrets"}),
                           title_score("hsrry potter chamb", {"title":"Batman Begins"}))


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.config={"TELEGRAM_ALLOWED_USER_ID":"1","TELEGRAM_CHAT_ID":"1", "RADARR_PROFILE_ID":"7",
                     "RADARR_MOVIES_ROOT":"/media/Movies", "RADARR_KIDS_ROOT":"/media/Kids/Movies"}
        self.bot=Bot(self.config, Path(self.tmp.name))
        self.bot.tg=Mock(return_value=True)
        self.bot.rad=Mock()
        self.movie={"id":42,"tmdbId":99,"title":"Example","qualityProfileId":7,"path":"/media/Movies/Example"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_unauthorised_message_does_nothing(self):
        self.bot.handle({"message":{"from":{"id":2},"chat":{"id":1,"type":"private"},"text":"Example"}})
        self.bot.rad.assert_not_called()
        self.bot.tg.assert_not_called()

    def test_stale_menu_cannot_download(self):
        self.bot.session("confirm",movie=self.movie,release=release())
        self.bot.handle({"callback_query":{"id":"c","from":{"id":1},"data":"old:download",
                       "message":{"chat":{"id":1,"type":"private"}}}})
        self.bot.rad.assert_not_called()

    def test_new_movie_added_unmonitored_without_auto_search(self):
        self.bot.rad.side_effect=[[],self.movie,{"records":[]},[release()]]
        self.bot.search_releases({"tmdbId":99,"title":"Example"})
        payload=self.bot.rad.call_args_list[1].args[1]
        self.assertFalse(payload["monitored"])
        self.assertFalse(payload["addOptions"]["searchForMovie"])
        self.assertEqual(self.bot.state["session"]["stage"],"confirm")

    def test_confirmation_persists_intent_before_grab(self):
        self.bot.session("confirm",movie=self.movie,release=release(),root='/media/Movies')
        def rad(endpoint,payload=None,**kwargs):
            if endpoint == 'movie/42':
                return self.movie
            if endpoint.startswith('queue?'):
                return {'records':[]}
            if endpoint.startswith("release?"):
                return [release()]
            persisted=__import__('json').loads(self.bot.path.read_text())
            self.assertIsNone(persisted["session"])
            self.assertEqual(persisted["jobs"]["42"]["status"],"submitting")
            return {}
        self.bot.rad.side_effect=rad
        self.bot.callback("download")
        self.assertEqual(self.bot.state["jobs"]["42"]["status"],"submitted")

    def test_uncertain_grab_is_not_retried(self):
        self.bot.session("confirm",movie=self.movie,release=release(),root='/media/Movies')
        self.bot.rad.side_effect=[self.movie,{'records':[]},[release()],ServiceError("timeout")]
        self.bot.callback("download")
        self.assertEqual(self.bot.rad.call_count,4)
        self.assertIn("check Radarr",self.bot.state["jobs"]["42"]["status"])
        self.assertIsNone(self.bot.state["session"])

    def test_alternatives_reuse_one_panel_and_keep_selection_local(self):
        releases=[release(guid=str(i)) for i in range(5)]
        self.bot.tg.return_value={'message_id':101}
        self.bot.show_recommendation(self.movie,releases,True)
        self.bot.tg.reset_mock()
        self.bot.callback('others:0')
        calls=self.bot.tg.call_args_list
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].args[0],'editMessageText')
        payload=calls[0].args[1]
        self.assertIn('Alternative 1 of 4',payload['text'])
        self.assertTrue(payload['reply_markup']['inline_keyboard'][0][0]['callback_data'].endswith(':use:1'))
        self.bot.callback('others:1')
        self.assertIn('Alternative 2 of 4',self.bot.tg.call_args.args[1]['text'])
        self.bot.rad.assert_not_called()

    def test_version_then_folder_then_confirmation_no_grab(self):
        self.bot.show_recommendation(self.movie,[release()],True)
        self.assertEqual(self.bot.state['session']['stage'],'confirm')
        self.assertEqual(self.bot.state['session']['root'],'/media/Movies')
        self.bot.callback('toggle')
        self.assertEqual(self.bot.state['session']['root'],'/media/Kids/Movies')
        self.assertEqual(self.bot.state['session']['stage'],'confirm')
        self.bot.rad.assert_not_called()
        text=self.bot.tg.call_args.args[1]['text']
        self.assertIn('Kids Movies',text)
        self.assertNotIn('/media/',text)

    def test_change_folder_and_back_to_versions(self):
        self.bot.show_recommendation(self.movie,[release()],True)
        self.bot.callback('toggle')
        self.bot.callback('back')
        self.assertEqual(self.bot.state['session']['root'],'/media/Kids/Movies')
        self.bot.callback('toggle')
        self.assertEqual(self.bot.state['session']['root'],'/media/Movies')

    def test_kids_destination_written_before_grab(self):
        self.bot.show_recommendation(self.movie,[release()],True)
        self.bot.callback('toggle')
        self.bot.rad.side_effect=[self.movie,{'records':[]},[release()],{},{}]
        self.bot.callback('download')
        put=self.bot.rad.call_args_list[3]
        self.assertEqual(put.args[0],'movie/42?moveFiles=false')
        self.assertEqual(put.args[1]['path'],'/media/Kids/Movies/Example')
        self.assertEqual(put.kwargs['method'],'PUT')
        self.assertEqual(self.bot.rad.call_args_list[4].args[0],'release')

    def test_existing_file_cannot_be_moved_or_grabbed(self):
        self.bot.session('confirm',movie=self.movie,release=release(),root='/media/Kids/Movies')
        self.bot.rad.side_effect=[{**self.movie,'hasFile':True},{'records':[]}]
        self.bot.callback('download')
        self.assertEqual(self.bot.rad.call_count,2)
        self.assertIsNone(self.bot.state['session'])

    def test_progress_button_works_without_a_live_menu(self):
        self.bot.state['session']=None
        self.bot.rad.side_effect=[[],{'records':[]}]
        self.bot.handle({'callback_query':{'id':'cb','from':{'id':1},'data':'global:status',
                       'message':{'chat':{'id':1,'type':'private'}}}})
        self.assertTrue(any('No current Downloads' in c.args[1].get('text','')
                            for c in self.bot.tg.call_args_list))

    def test_poster_metadata_does_not_spawn_separate_messages(self):
        self.bot.show_recommendation({**self.movie,'remotePoster':'https://example.org/poster.jpg'},[release()],True)
        self.assertEqual(self.bot.tg.call_args.args[0],'sendPhoto')
        self.assertEqual(self.bot.tg.call_count,1)

    def test_import_notification_requires_file(self):
        self.bot.state["jobs"]["42"]={"title":"Example","status":"submitted"}
        self.bot.rad.side_effect=[{'records':[]},{'hasFile':False}]
        self.bot.check_imports()
        self.assertFalse(any('is ready' in c.args[1].get('text','') for c in self.bot.tg.call_args_list))
        self.bot.rad.side_effect=[{'records':[]},{'hasFile':True,'movieFileId':5}]
        self.bot.check_imports()
        self.assertEqual(self.bot.state["jobs"]["42"]["status"],"imported")
        before=self.bot.tg.call_count
        self.bot.check_imports()
        self.assertEqual(self.bot.tg.call_count,before)


if __name__ == "__main__":
    unittest.main()
