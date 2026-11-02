import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from bot import Bot, Qbit, ServiceError


HASH = 'a'*40


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        config = {'TELEGRAM_ALLOWED_USER_ID':'1','TELEGRAM_CHAT_ID':'1','RADARR_PROFILE_ID':'7',
                  'RADARR_MOVIES_ROOT':str(root/'Movies'),'RADARR_KIDS_ROOT':str(root/'Kids/Movies')}
        self.bot = Bot(config,root/'state')
        self.bot.tg = Mock(return_value={'message_id':12})
        self.movie = {'id':42,'tmdbId':99,'title':'A movie with a very long title','year':2003,
                      'added':'2026-11-02T16:00:00Z','path':str(root/'Movies/Example'),'monitored':True}
        self.other = dict(self.movie,id=43,tmdbId=100,title='Other movie',path=str(root/'Movies/Other'))
        self.queue = [{'id':8,'movieId':42,'downloadId':HASH,'size':100,'sizeleft':50,'status':'downloading'}]
        self.history = [{'movieId':42,'downloadId':HASH}]
        self.configs = [{'enable':True,'implementation':'QBittorrent'}]
        self.deleted = False
        self.events = []
        def rad(endpoint,payload=None,**kw):
            method = kw.get('method')
            if method:
                self.events.append((method,endpoint,payload))
            if endpoint.startswith('movie/42'):
                if method=='DELETE':
                    self.deleted=True
                    return {}
                if self.deleted:
                    raise ServiceError('missing',status=404)
                return dict(self.movie)
            if endpoint=='movie': return [dict(self.movie),dict(self.other)]
            if endpoint.startswith('queue?'): return {'records':copy.deepcopy(self.queue)}
            if endpoint.startswith('queue/'): return {}
            if endpoint=='config/mediamanagement': return {'recycleBin':''}
            if endpoint.startswith('history/movie'): return copy.deepcopy(self.history)
            if endpoint.startswith('history?'): return {'records':copy.deepcopy(self.history)}
            if endpoint=='downloadclient': return self.configs
            raise AssertionError(endpoint)
        self.bot.rad=Mock(side_effect=rad)
        self.client=Mock()
        self.client.category='radarr'
        self.client.data_paths.return_value=set()
        self.client.torrents.return_value=[{'hash':HASH,'category':'radarr','content_path':'/staging/movie.mkv'}]
        def remove(h):
            self.events.append(('QBIT_DELETE',h,True))
            self.client.torrents.return_value=[]
        self.client.delete.side_effect=remove
        self.qpatch=patch('bot.Qbit',return_value=self.client)
        self.qpatch.start()
        self.bot.state['jobs']['42']={'title':self.movie['title'],'started':time.time(),'status':'submitted',
                                     'identity':self.bot.movie_identity(self.movie),'ui':{'message_id':55,'photo':False}}

    def tearDown(self):
        self.qpatch.stop()
        self.tmp.cleanup()

    def prompt(self):
        token=self.bot.movie_target(self.movie)
        self.bot.ask_delete(token)
        return next(iter(self.bot.state['delete_prompts']))

    def click(self,data,uid=1):
        self.bot.handle({'callback_query':{'id':'cb','from':{'id':uid},'data':data,
                        'message':{'chat':{'id':1,'type':'private'}}}})

    def test_first_tap_only_displays_exact_named_confirmation(self):
        self.prompt()
        self.assertEqual(self.events,[])
        payload=next(c.args[1] for c in reversed(self.bot.tg.call_args_list) if c.args[0] in ('sendMessage','editMessageText'))
        self.assertIn(self.movie['title'],payload['text'])
        self.assertIn('2003',payload['text'])

    def test_success_deletes_exact_torrent_before_radarr_and_keeps_other_job(self):
        self.bot.state['jobs']['43']={'title':'Other','status':'submitted'}
        self.bot.state['watches']['99']={'candidate':self.movie}
        self.bot.state['watches']['100']={'candidate':self.other}
        self.bot.delete_confirmed(self.prompt())
        self.assertEqual(self.events[0][0],'PUT')
        self.assertFalse(self.events[0][2]['monitored'])
        self.assertEqual(self.events[1],('QBIT_DELETE',HASH,True))
        self.assertIn('removeFromClient=false&blocklist=false&skipRedownload=true',self.events[2][1])
        self.assertEqual(self.events[3][:2],('DELETE','movie/42?deleteFiles=true&addImportExclusion=false'))
        self.assertNotIn('42',self.bot.state['jobs'])
        self.assertIn('43',self.bot.state['jobs'])
        self.assertNotIn('99',self.bot.state['watches'])
        self.assertIn('100',self.bot.state['watches'])

    def test_duplicate_confirmation_does_not_replay_mutations(self):
        nonce=self.prompt()
        self.bot.delete_confirmed(nonce)
        count=len(self.events)
        self.bot.delete_confirmed(nonce)
        self.assertEqual(len(self.events),count)

    def test_keep_button_does_not_touch_download(self):
        nonce=self.prompt()
        self.click('global:keep:'+nonce)
        self.bot.delete_confirmed(nonce)
        self.assertEqual(self.events,[])

    def test_other_user_cannot_confirm(self):
        nonce=self.prompt()
        self.click('global:erase:'+nonce,uid=2)
        self.assertEqual(self.events,[])
        self.assertIn(nonce,self.bot.state['delete_prompts'])

    def test_expired_confirmation_cannot_delete(self):
        nonce=self.prompt()
        self.bot.state['delete_prompts'][nonce]['expires']=0
        self.bot.delete_confirmed(nonce)
        self.assertEqual(self.events,[])

    def test_movie_id_reuse_or_path_change_refused(self):
        nonce=self.prompt()
        self.movie['tmdbId']=777
        self.bot.delete_confirmed(nonce)
        self.assertEqual(self.events,[])

    def test_root_folder_cannot_be_deleted(self):
        self.movie['path']=self.bot.c['RADARR_MOVIES_ROOT']
        self.bot.ask_delete(self.bot.movie_target(self.movie))
        self.assertEqual(self.events,[])
        self.assertEqual(self.bot.state['delete_prompts'],{})

    def test_shared_queue_hash_refused(self):
        self.queue.append({'id':9,'movieId':43,'downloadId':HASH})
        self.bot.delete_confirmed(self.prompt())
        self.assertEqual(self.events,[])

    def test_shared_history_hash_refused(self):
        self.history.append({'movieId':43,'downloadId':HASH})
        self.bot.delete_confirmed(self.prompt())
        self.assertEqual(self.events,[])

    def test_shared_library_folder_refused(self):
        self.other['path']=self.movie['path']
        self.bot.delete_confirmed(self.prompt())
        self.assertEqual(self.events,[])

    def test_shared_torrent_content_refused(self):
        self.client.torrents.return_value.append({'hash':'b'*40,'content_path':'/staging/movie.mkv'})
        self.bot.delete_confirmed(self.prompt())
        self.assertEqual(self.events,[])

    def test_changed_category_refused(self):
        self.client.torrents.return_value[0]['category']='another-app'
        self.bot.delete_confirmed(self.prompt())
        self.assertEqual(self.events,[])

    def test_failed_torrent_removal_keeps_radarr_entry_and_tracking_for_retry(self):
        self.client.delete.side_effect=ServiceError('Torrent removal pending')
        self.bot.delete_confirmed(self.prompt())
        self.assertFalse(self.deleted)
        self.assertIn('42',self.bot.state['jobs'])
        self.assertIn('42',self.bot.state['deletions'])
        self.assertIn('incomplete',self.bot.tg.call_args.args[1]['text'])

    def test_radarr_async_file_cleanup_must_finish_before_success(self):
        folder=Path(self.movie['path'])
        folder.mkdir(parents=True)
        file=folder/'movie.mkv'
        file.write_bytes(b'test')
        self.bot.delete_confirmed(self.prompt())
        self.assertTrue(self.deleted)
        self.assertIn('42',self.bot.state['deletions'])
        self.assertIn('42',self.bot.state['jobs'])
        self.assertIn('removal requested',self.bot.tg.call_args.args[1]['text'])
        file.unlink()
        folder.rmdir()
        self.events.clear()
        self.bot.check_deletions()
        self.assertNotIn('42',self.bot.state['deletions'])
        self.assertNotIn('42',self.bot.state['jobs'])
        self.assertEqual(self.events,[])  # Background verification performs no destructive retries.

    def test_pending_deletion_stays_visible_after_radarr_entry_disappears(self):
        self.bot.state['deletions']['42']={'identity':self.bot.movie_identity(self.movie),
            'title':self.movie['title'],'hashes':[HASH],'phase':'removing_radarr'}
        old=self.bot.rad.side_effect
        self.bot.rad.side_effect=lambda endpoint,*a,**kw: [self.other] if endpoint=='movie' else old(endpoint,*a,**kw)
        self.bot.show_downloads()
        texts=[str(c.args[1]) for c in self.bot.tg.call_args_list]
        self.assertTrue(any('pending' in t.lower() for t in texts))

    def test_incomplete_torrent_file_cleanup_prevents_success(self):
        file=Path(self.tmp.name)/'partial.mkv.!qB'
        file.write_bytes(b'test')
        self.client.data_paths.return_value={str(file)}
        self.bot.delete_confirmed(self.prompt())
        self.assertIn('42',self.bot.state['deletions'])
        file.unlink()
        self.bot.check_deletions()
        self.assertNotIn('42',self.bot.state['deletions'])

    def test_missing_hash_does_not_guess_using_title(self):
        self.queue=[]
        self.history=[]
        self.bot.delete_confirmed(self.prompt())
        self.assertEqual(self.events,[])

    def test_unavailable_client_does_not_remove_radarr_movie(self):
        self.client.torrents.side_effect=ServiceError('offline')
        self.bot.delete_confirmed(self.prompt())
        self.assertEqual(self.events,[])

    def test_existing_downloads_survive_restart(self):
        self.bot.save()
        resumed=Bot(self.bot.c,self.bot.directory)
        self.assertEqual(resumed.state['jobs'],self.bot.state['jobs'])

    def test_master_list_shows_non_bot_download_and_individual_delete_buttons(self):
        self.queue.append({'id':9,'movieId':43,'downloadId':'b'*40,'size':100,'sizeleft':25})
        self.bot.show_downloads()
        cards=[c.args[1] for c in self.bot.tg.call_args_list if c.args[0] in ('sendMessage','editMessageText')]
        self.assertEqual(len(cards),1)
        self.assertIn('50%',str(cards[0]['reply_markup']))
        self.assertIn('75%',str(cards[0]['reply_markup']))
        rows=cards[0]['reply_markup']['inline_keyboard'][:2]
        self.assertTrue(all(len(row)==1 for row in rows))
        buttons=[row[0]['callback_data'] for row in rows]
        self.assertNotEqual(buttons[0],buttons[1])
        for button,movie in zip(buttons,[self.movie,self.other]):
            self.assertEqual(self.bot.state['movie_targets'][button.split(':')[2]]['id'],movie['id'])
        self.assertEqual(self.events,[])

    def test_list_refresh_does_not_clear_approval_or_search_session(self):
        nonce=self.prompt()
        self.bot.session('movie',movies=[self.other])
        original=copy.deepcopy(self.bot.state['session'])
        self.bot.show_downloads()
        self.assertEqual(self.bot.state['session'],original)
        self.assertIn(nonce,self.bot.state['delete_prompts'])

    def test_imported_library_card_has_delete_but_does_not_cancel_other_download(self):
        self.other['hasFile']=True
        self.bot.show_downloads(library=True)
        cards=[c.args[1] for c in self.bot.tg.call_args_list if c.args[0]=='sendMessage'
               and c.args[1]['text'].startswith('✅ Library')]
        self.assertEqual(len(cards),1)
        self.assertIn('Other movie',cards[0]['text'])
        self.assertNotIn('global:delete:',str(cards[0]['reply_markup']))
        self.assertEqual(self.events,[])


class QbitDeleteTests(unittest.TestCase):
    def test_file_snapshot_includes_final_and_incomplete_locations(self):
        q=object.__new__(Qbit)
        q.call=Mock(return_value=[{'name':'Example/movie.mkv'}])
        paths=q.data_paths({'hash':HASH,'save_path':'/staging/complete','download_path':'/staging/current'})
        self.assertEqual(paths,{'/staging/complete/Example/movie.mkv','/staging/complete/Example/movie.mkv.!qB',
                                '/staging/current/Example/movie.mkv','/staging/current/Example/movie.mkv.!qB'})

    def test_file_snapshot_refuses_paths_escaping_download_folder(self):
        q=object.__new__(Qbit)
        q.call=Mock(return_value=[{'name':'../../another/movie.mkv'}])
        with self.assertRaises(ServiceError):
            q.data_paths({'hash':HASH,'save_path':'/staging/current'})

    def test_only_exact_valid_hash_sent_with_delete_files_true(self):
        q=object.__new__(Qbit)
        q.call=Mock(return_value='')
        q.torrents=Mock(return_value=[])
        q.delete(HASH)
        self.assertEqual(q.call.call_args.args,('torrents/delete',{'hashes':HASH,'deleteFiles':'true'}))
        with self.assertRaises(ServiceError): q.delete('all')
        self.assertEqual(q.call.call_count,1)

    def test_async_delete_is_not_reported_complete(self):
        q=object.__new__(Qbit)
        q.call=Mock(return_value='')
        q.torrents=Mock(return_value=[{'hash':HASH}])
        with self.assertRaises(ServiceError): q.delete(HASH)


if __name__=='__main__':
    unittest.main()
