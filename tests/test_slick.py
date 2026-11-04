import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from bot import Bot, ServiceError
from test_bot import release


class SlickTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        self.config={'TELEGRAM_ALLOWED_USER_ID':'1','TELEGRAM_CHAT_ID':'1','RADARR_PROFILE_ID':'7',
                     'RADARR_MOVIES_ROOT':str(root/'Movies'),'RADARR_KIDS_ROOT':str(root/'Kids/Movies')}
        self.bot=Bot(self.config,root/'state')
        self.bot.tg=Mock(return_value={'message_id':101})
        self.bot.rad=Mock()
        self.movie={'id':42,'tmdbId':99,'title':'Example','year':2008,'qualityProfileId':7,
                    'path':str(root/'Movies/Example'),'monitored':False,'added':'2026-11-02T17:00:00Z',
                    'minimumAvailability':'released','hasFile':False,'tags':[]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_main_card_is_edited_instead_of_adding_messages(self):
        self.bot.panel('Searching')
        self.bot.show_recommendation(self.movie,[release()],True)
        self.assertEqual(self.bot.tg.call_args_list[0].args[0],'sendMessage')
        self.assertEqual(self.bot.tg.call_args_list[1].args[0],'editMessageText')
        self.assertEqual(self.bot.tg.call_args_list[1].args[1]['message_id'],101)

    def test_recommendation_has_download_and_default_destination(self):
        self.bot.show_recommendation(self.movie,[release()],True)
        self.assertEqual(self.bot.state['session']['root'],self.config['RADARR_MOVIES_ROOT'])
        markup=self.bot.tg.call_args.args[1]['reply_markup']['inline_keyboard']
        self.assertEqual(markup[0][0]['text'],'Download')
        self.assertEqual([[button['text'] for button in row] for row in markup],
                         [['Download', 'Save to Kids'],
                          ['Other versions', 'Technical details'], ['Main menu']])
        self.bot.rad.assert_not_called()

    def test_destination_toggle_is_local_until_download(self):
        self.bot.show_recommendation(self.movie,[release()],True)
        self.bot.callback('toggle')
        self.assertEqual(self.bot.state['session']['root'],self.config['RADARR_KIDS_ROOT'])
        self.bot.rad.assert_not_called()

    def test_poster_caption_is_edited_on_toggle(self):
        self.bot.show_recommendation({**self.movie,'remotePoster':'https://example.org/a.jpg'},[release()],True)
        self.bot.callback('toggle')
        self.assertEqual(self.bot.tg.call_args.args[0],'editMessageCaption')
        self.assertIn('Kids Movies',self.bot.tg.call_args.args[1]['caption'])

    def test_identical_panel_does_not_trigger_not_modified_error(self):
        self.bot.panel('Same content')
        self.bot.panel('Same content')
        self.assertEqual(self.bot.tg.call_count,1)

    def test_alternative_selection_preserves_kids_destination(self):
        self.bot.show_recommendation(self.movie,[release(),release(guid='second')],True)
        self.bot.callback('toggle')
        self.bot.callback('others:0')
        self.bot.callback('use:1')
        self.assertEqual(self.bot.state['session']['selected'],1)
        self.assertEqual(self.bot.state['session']['root'],self.config['RADARR_KIDS_ROOT'])
        self.bot.rad.assert_not_called()

    def test_alternative_selection_stays_in_same_message(self):
        self.bot.show_recommendation(self.movie,[release(),release(guid='second')],True)
        self.bot.callback('others:0')
        self.bot.callback('use:1')
        self.assertEqual(sum(c.args[0]=='sendMessage' for c in self.bot.tg.call_args_list),1)
        self.assertEqual(self.bot.state['ui']['message_id'],101)

    def test_background_progress_is_stored_without_separate_card(self):
        self.bot.state['jobs']['42']={'title':'Example','status':'submitted','started':time.time()}
        queue={'records':[{'movieId':42,'size':1000,'sizeleft':500,'status':'downloading'}]}
        self.bot.rad.side_effect=[queue,self.movie]
        self.bot.check_downloads()
        self.bot.tg.assert_not_called()
        self.assertIn('50%',self.bot.state['jobs']['42']['progress'])

    def test_background_progress_does_not_replace_active_search(self):
        self.bot.state['ui']={'message_id':99,'photo':False,'signature':'search'}
        job={'ui':{'message_id':55,'photo':False}}
        self.bot.publish_job(job,'50%','42')
        self.assertEqual(self.bot.state['ui']['message_id'],99)

    def test_progress_suppresses_unchanged_updates(self):
        job={'ui':{'message_id':55,'photo':False}}
        self.bot.publish_job(job,'50%','42')
        self.bot.publish_job(job,'50%','42')
        self.assertEqual(self.bot.tg.call_count,0)

    def test_warning_notifies_once_until_recovered(self):
        self.bot.state['jobs']['42']={'title':'Example','status':'submitted','started':time.time(),
                                     'ui':{'message_id':55,'photo':False}}
        queue={'records':[{'movieId':42,'status':'completed','trackedDownloadStatus':'warning'}]}
        self.bot.rad.side_effect=[queue,self.movie,queue,self.movie]
        self.bot.check_downloads()
        self.bot.check_downloads()
        alerts=[c for c in self.bot.tg.call_args_list if c.args[0]=='sendMessage']
        self.assertEqual(len(alerts),0)
        self.assertIn('warning:42',self.bot.state['notices'])

    def test_ready_requires_import_and_sends_one_notification(self):
        self.bot.state['jobs']['42']={'title':'Example','status':'submitted','started':time.time(),
                                     'ui':{'message_id':55,'photo':False}}
        self.bot.rad.side_effect=[{'records':[]},{**self.movie,'hasFile':True,'movieFileId':9}]
        self.bot.check_downloads()
        self.bot.check_downloads()
        self.assertEqual(self.bot.state['jobs']['42']['status'],'imported')
        self.assertEqual(sum(c.args[0]=='sendMessage' for c in self.bot.tg.call_args_list),0)
        self.assertIn('ready:42',self.bot.state['notices'])

    def test_availability_alert_opt_in_preserves_no_preview(self):
        self.bot.remember_preview(self.movie)
        self.bot.session('watch_offer',movie=self.movie)
        self.bot.rad.side_effect=[{'records':[]},self.movie,{}]
        self.bot.callback('watch')
        self.assertIn('99',self.bot.state['watches'])
        self.assertNotIn('42',self.bot.state['previews'])
        self.assertEqual(self.bot.rad.call_args.kwargs,{'method':'DELETE'})
        self.assertFalse(any(c.args[0]=='release' for c in self.bot.rad.call_args_list))

    def test_watches_pause_while_user_browses(self):
        self.bot.state['watches']['99']={'candidate':self.movie,'next_check':0}
        self.bot.session('movie',movies=[self.movie])
        self.bot.check_watches()
        self.bot.rad.assert_not_called()

    def test_watch_match_notifies_without_downloading(self):
        self.bot.state['watches']['99']={'candidate':self.movie,'next_check':0}
        self.bot.rad.side_effect=[[self.movie],{'records':[]},[release()]]
        self.bot.check_watches()
        self.assertTrue(self.bot.state['watches']['99']['notified'])
        self.assertIn('watch:99',self.bot.state['notices'])
        self.bot.tg.assert_not_called()
        self.assertFalse(any(len(c.args)>1 and c.args[1] is not None for c in self.bot.rad.call_args_list))

    def test_watch_created_metadata_is_cleaned_after_search(self):
        self.bot.state['watches']['99']={'candidate':self.movie,'next_check':0}
        self.bot.rad.side_effect=[[],self.movie,{'records':[]},[release()],{'records':[]},self.movie,{}]
        self.bot.check_watches()
        self.assertEqual(self.bot.state['previews'],{})
        self.assertEqual(self.bot.rad.call_args.kwargs,{'method':'DELETE'})
        add=self.bot.rad.call_args_list[1]
        self.assertFalse(add.args[1]['monitored'])
        self.assertFalse(add.args[1]['addOptions']['searchForMovie'])

    def test_unauthorised_global_callback_is_ignored(self):
        self.bot.state['watches']['99']={'candidate':self.movie,'next_check':0}
        self.bot.handle({'callback_query':{'id':'evil','from':{'id':2},'data':'global:unwatch:99',
                        'message':{'chat':{'id':1,'type':'private'}}}})
        self.assertIn('99',self.bot.state['watches'])
        self.bot.tg.assert_not_called()

    def test_watch_can_be_removed_without_touching_radarr(self):
        self.bot.state['watches']['99']={'candidate':self.movie,'next_check':0}
        self.bot.handle({'callback_query':{'id':'ok','from':{'id':1},'data':'global:unwatch:99',
                        'message':{'chat':{'id':1,'type':'private'}}}})
        self.assertEqual(self.bot.state['watches'],{})
        self.bot.rad.assert_not_called()


if __name__=='__main__':
    unittest.main()
