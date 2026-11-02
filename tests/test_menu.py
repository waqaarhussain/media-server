import copy
import unittest
from unittest.mock import Mock
import test_delete
from bot import ServiceError
from test_bot import release


class TelegramScreen:
    def __init__(self):
        self.messages={}
        self.next_id=300
    def __call__(self,method,payload):
        if method=='answerCallbackQuery': return True
        if method in ('sendMessage','sendPhoto'):
            self.next_id+=1
            self.messages[self.next_id]=dict(payload)
            return {'message_id':self.next_id}
        mid=payload.get('message_id')
        if mid not in self.messages:
            raise ServiceError('Message not found.',status=400)
        if method=='deleteMessage':
            self.messages.pop(mid)
            return True
        if method in ('editMessageText','editMessageCaption','editMessageMedia'):
            changed={k:v for k,v in payload.items() if k!='message_id'}
            if changed==self.messages[mid]:
                raise ServiceError('Message unchanged.',status=400)
            self.messages[mid]=changed
            return {'message_id':mid}
        raise AssertionError(method)


class MenuTests(unittest.TestCase):
    tearDown=test_delete.DeleteTests.tearDown
    click=test_delete.DeleteTests.click
    def setUp(self):
        test_delete.DeleteTests.setUp(self)
        self.bot.state['jobs']['42'].pop('ui',None)
        self.telegram=TelegramScreen()
        self.bot.tg=Mock(side_effect=self.telegram)
    def message(self,text):
        self.bot.handle({'message':{'from':{'id':1},'chat':{'id':1,'type':'private'},'text':text}})
    def current(self):
        self.assertEqual(len(self.telegram.messages),1)
        return self.telegram.messages[self.bot.state['ui']['message_id']]

    def test_explicit_start_replaces_old_panel_but_buttons_reuse_the_new_one(self):
        self.message('/start')
        old=self.bot.state['ui']['message_id']
        self.message('/start')
        self.message('/menu')
        mid=self.bot.state['ui']['message_id']
        self.assertNotEqual(mid,old)
        self.click('global:home')
        self.assertEqual(self.bot.state['ui']['message_id'],mid)
        self.assertIn('Movie menu',self.current()['text'])
        self.assertEqual(sum(c.args[0]=='sendMessage' for c in self.bot.tg.call_args_list),3)

    def test_deleted_message_is_replaced_once(self):
        self.message('/start')
        old=self.bot.state['ui']['message_id']
        self.telegram.messages.pop(old)
        self.message('/start')
        self.assertNotEqual(self.bot.state['ui']['message_id'],old)
        self.current()
        self.message('/start')
        self.current()
        self.assertEqual(sum(c.args[0]=='sendMessage' for c in self.bot.tg.call_args_list),3)

    def test_poster_is_only_on_movie_card_and_cleared_on_main_menu(self):
        self.message('/start')
        mid=self.bot.state['ui']['message_id']
        movie=dict(self.movie,remotePoster='https://example.org/poster.jpg')
        self.bot.show_recommendation(movie,[release()],True)
        self.assertEqual(self.bot.state['ui']['message_id'],mid)
        self.assertTrue(self.bot.state['ui']['photo'])
        self.assertTrue(any(c.args[0]=='editMessageMedia' for c in self.bot.tg.call_args_list))
        self.click('global:home')
        self.assertFalse(self.bot.state['ui']['photo'])
        self.assertIn('Movie menu',self.current()['text'])
        self.assertEqual(len(self.telegram.messages),1)

    def test_start_displays_menu_without_waiting_for_radarr(self):
        self.bot.rad.side_effect=ServiceError('Radarr offline')
        self.message('/start')
        self.bot.rad.assert_not_called()
        self.assertIn('Movie menu',self.current()['text'])

    def test_start_recovers_panel_hidden_only_on_phone(self):
        self.message('/start')
        hidden=self.bot.state['ui']['message_id']
        # The old message still exists on Telegram's side, but the client has hidden it.
        self.assertIn(hidden,self.telegram.messages)
        self.message('/start')
        self.assertNotIn(hidden,self.telegram.messages)
        self.assertIn('Movie menu',self.current()['text'])

    def test_all_main_menu_routes_use_same_screen_and_keep_downloads(self):
        jobs=copy.deepcopy(self.bot.state['jobs'])
        self.other['hasFile']=True
        self.message('/start')
        mid=self.bot.state['ui']['message_id']
        for action in ('global:downloads:0','global:library:0','global:manage:0','global:watchlist','global:search','global:home'):
            self.click(action)
            self.assertEqual(self.bot.state['ui']['message_id'],mid)
            self.current()
        self.assertEqual(self.bot.state['jobs'],jobs)
        self.assertEqual(self.events,[])

    def test_main_menu_has_search_downloads_library_delete(self):
        self.message('/start')
        markup=str(self.current()['reply_markup'])
        for action in ('global:search','global:downloads:0','global:library:0','global:manage:0'):
            self.assertIn(action,markup)

    def test_delete_entry_prompts_for_name_instead_of_listing_library(self):
        self.message('/start')
        self.click('global:manage:0')
        self.assertIn('Type any part',self.current()['text'])
        self.assertNotIn(self.movie['title'],str(self.current()))
        self.assertEqual(self.events,[])

    def test_details_delete_confirmation_and_back_return_to_exact_movie_then_list(self):
        self.message('/start')
        self.click('global:downloads:0')
        token=self.bot.movie_target(self.movie)
        self.click('global:item:'+token)
        self.assertEqual(self.bot.state['view']['route'],'detail')
        self.click('global:delete:'+token)
        self.assertIn(self.movie['title'],self.current()['text'])
        nonce=next(iter(self.bot.state['delete_prompts']))
        self.click('global:keep:'+nonce)
        self.assertEqual(self.bot.state['view']['route'],'detail')
        self.click('global:back')
        self.assertEqual(self.bot.state['view']['route'],'list')
        self.assertEqual(self.events,[])
        self.current()

    def test_background_progress_and_ready_do_not_create_messages_or_hijack_search(self):
        self.message('/start')
        self.click('global:search')
        screen=copy.deepcopy(self.current())
        self.bot.check_downloads()
        self.bot.refresh_visible()
        self.assertEqual(self.current(),screen)
        self.movie.update(hasFile=True,movieFileId=7)
        self.bot.check_downloads()
        self.bot.refresh_visible()
        self.assertEqual(self.current(),screen)
        self.assertIn('ready:42',self.bot.state['notices'])
        self.click('global:home')
        self.assertIn('is ready',self.current()['text'])

    def test_background_does_not_replace_delete_confirmation(self):
        self.message('/start')
        self.click('global:downloads:0')
        self.click('global:delete:'+self.bot.movie_target(self.movie))
        screen=copy.deepcopy(self.current())
        self.bot.check_downloads()
        self.bot.refresh_visible()
        self.assertEqual(self.current(),screen)
        self.assertEqual(self.events,[])

    def test_alternatives_and_technical_details_stay_in_one_screen(self):
        self.message('/start')
        releases=[release(guid=str(i)) for i in range(4)]
        self.bot.show_recommendation(self.movie,releases,True)
        for action in ('others:0','others:1','details:2','back','toggle'):
            self.bot.callback(action)
            self.current()
        self.assertEqual(sum(c.args[0]=='sendMessage' for c in self.bot.tg.call_args_list),1)
        self.assertEqual(self.events,[])

    def test_confirmed_deletion_reuses_panel_and_leaves_other_job(self):
        self.bot.state['jobs']['43']={'title':'Other','status':'submitted'}
        self.message('/start')
        self.click('global:downloads:0')
        self.click('global:delete:'+self.bot.movie_target(self.movie))
        nonce=next(iter(self.bot.state['delete_prompts']))
        self.click('global:erase:'+nonce)
        self.assertIn('removed',self.current()['text'])
        self.assertNotIn('42',self.bot.state['jobs'])
        self.assertIn('43',self.bot.state['jobs'])

    def test_connection_failure_does_not_create_duplicate_panel(self):
        self.message('/start')
        old=self.bot.tg.side_effect
        self.bot.tg.side_effect=lambda method,payload: (_ for _ in ()).throw(ServiceError('offline')) if method=='editMessageText' else old(method,payload)
        with self.assertRaises(ServiceError):
            self.bot.panel('Changed')
        self.current()
        self.assertEqual(sum(c.args[0]=='sendMessage' for c in self.bot.tg.call_args_list),1)


if __name__=='__main__':
    unittest.main()
