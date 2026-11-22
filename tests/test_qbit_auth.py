"""Exercise the real urllib cookie jar for old and new qBittorrent login replies."""
import http.cookiejar
from email.message import Message
import io
import json
import unittest
import urllib.parse
import urllib.request
import urllib.response
from unittest.mock import patch

import bot
import qbit_migrate as q


class QbitAPI(urllib.request.HTTPSHandler):
    def __init__(self,status=204,body=b'',cookie_name='QBT_SID_8080'):
        super().__init__()
        self.login_status=status;self.login_body=body;self.cookie_name=cookie_name;self.calls=[]
    def https_open(self,req):
        path=urllib.parse.urlsplit(req.full_url).path
        self.calls.append((req.get_method(),path))
        headers=Message();headers['Content-Type']='application/json'
        if path.endswith('/auth/login'):
            status=self.login_status;body=self.login_body
            if status==204 or body==b'Ok.':
                headers['Set-Cookie']=self.cookie_name+'=test-session; HttpOnly; Secure; Path=/'
        elif req.get_header('Cookie')!=self.cookie_name+'=test-session':
            status=403;body=b'No valid session'
        else:
            status=200
            body={
                '/api/v2/app/version':b'v5.2.4',
                '/api/v2/app/preferences':b'{"up_limit":123}',
                '/api/v2/torrents/categories':b'{"Kids Movies":{"savePath":"/old/kids"}}',
                '/api/v2/torrents/info':b'[]',
            }.get(path,b'{}')
        response=urllib.response.addinfourl(io.BytesIO(body),headers,req.full_url,status)
        response.msg='fixture response'
        return response


def opener(api):
    return urllib.request.build_opener(api,urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


class LoginCompatibilityTests(unittest.TestCase):
    def test_backup_accepts_204_and_reuses_port_qualified_session_cookie_for_settings_reads(self):
        api=QbitAPI();client=q.Client('https://example.test');client.opener=opener(api)
        client.login('admin','test-password')
        self.assertEqual(client.last_status,204)
        snapshot=q.capture(client)
        self.assertEqual(snapshot['preferences']['up_limit'],123)
        self.assertIn('Kids Movies',snapshot['categories'])
        self.assertEqual(api.calls,[('POST','/api/v2/auth/login'),('GET','/api/v2/app/version'),
                                    ('GET','/api/v2/app/preferences'),('GET','/api/v2/torrents/categories')])

    def test_backup_keeps_support_for_200_ok_and_legacy_sid_cookie(self):
        api=QbitAPI(200,b'Ok.','SID');client=q.Client('https://example.test');client.opener=opener(api)
        client.login('admin','test-password')
        self.assertEqual(q.capture(client)['preferences']['up_limit'],123)

    def test_empty_200_is_not_accepted_as_a_valid_login(self):
        api=QbitAPI(200,b'');client=q.Client('https://example.test');client.opener=opener(api)
        with self.assertRaisesRegex(q.RequestError,'unexpected login response'):client.login('admin','test-password')

    def test_bad_credentials_remain_rejected_for_both_api_generations(self):
        for status,body in ((200,b'Fails.'),(401,b'Unauthorized')):
            api=QbitAPI(status,body);client=q.Client('https://example.test');client.opener=opener(api)
            with self.subTest(status=status),self.assertRaises(q.RequestError):client.login('admin','wrong')
            self.assertEqual(api.calls,[('POST','/api/v2/auth/login')])

    def test_empty_204_does_not_make_a_backup_if_the_cookie_cannot_authenticate_reads(self):
        api=QbitAPI();client=q.Client('https://example.test');client.opener=opener(api)
        client.login('admin','test-password')
        # Removing the session proves protected API reads still reject invalid sessions.
        client.opener=opener(api)
        with self.assertRaisesRegex(q.RequestError,'HTTP 403'):q.capture(client)

    def test_bot_uses_the_new_cookie_for_download_client_reads(self):
        api=QbitAPI();http=opener(api)
        resource={'fields':[{'name':n,'value':v} for n,v in
                  {'host':'example.test','port':443,'useSsl':True,'category':'Movies',
                   'username':'admin','password':'test-password'}.items()]}
        with patch.object(bot.urllib.request,'build_opener',return_value=http):
            client=bot.Qbit(resource)
            self.assertEqual(client.torrents(),[])
        self.assertEqual(api.calls,[('POST','/api/v2/auth/login'),('GET','/api/v2/torrents/info')])


if __name__=='__main__':unittest.main()
