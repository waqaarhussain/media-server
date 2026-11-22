import contextlib
import io
import json
import socket
import ssl
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock,patch

import qbit_migrate as q


class ConnectionDiagnosticsTests(unittest.TestCase):
    def test_request_uses_matching_origin_form_headers_and_non_default_user_agent(self):
        client=q.Client('https://example.test:444/qbit/')
        response=io.BytesIO(b'Ok.')
        client.opener=Mock();client.opener.open.return_value=response
        self.assertEqual(client.call('auth/login',{'username':'admin','password':'private-password'}),'Ok.')
        req=client.opener.open.call_args.args[0]
        self.assertEqual(req.full_url,'https://example.test:444/qbit/api/v2/auth/login')
        self.assertEqual(req.get_header('Origin'),'https://example.test:444')
        self.assertEqual(req.get_header('Referer'),'https://example.test:444/qbit/')
        self.assertEqual(req.get_header('Content-type'),'application/x-www-form-urlencoded')
        self.assertEqual(req.get_header('User-agent'),'media-server-backup/1.0')

    def test_http_errors_report_endpoint_status_without_raw_urls_or_body(self):
        client=q.Client('https://example.test')
        client.opener=Mock()
        for code in (401,403,404,405,429,502):
            error=urllib.error.HTTPError('https://example.test/?secret=private-key',code,'private-password',{},io.BytesIO(b'private-response'))
            client.opener.open.side_effect=error
            with self.subTest(code=code),self.assertRaises(q.RequestError) as caught:
                client.call('app/preferences',json_result=True)
            message=str(caught.exception)
            self.assertIn('app/preferences',message);self.assertIn(str(code),message)
            self.assertNotIn('private',message);self.assertNotIn('secret=',message)

    def test_dns_tls_and_refused_connection_are_distinguished_without_echoing_details(self):
        client=q.Client('https://example.test');client.opener=Mock()
        cases=[(socket.gaierror('private-host'),'resolved'),
               (ssl.SSLCertVerificationError('private-host'),'certificate'),
               (ConnectionRefusedError('private-host'),'refused')]
        for reason,expected in cases:
            client.opener.open.side_effect=urllib.error.URLError(reason)
            with self.subTest(expected=expected),self.assertRaises(q.RequestError) as caught:client.call('auth/login')
            self.assertIn(expected,str(caught.exception));self.assertNotIn('private-host',str(caught.exception))

    def test_html_proxy_or_login_pages_are_not_misreported_as_bad_credentials(self):
        client=q.Client('https://example.test');client.opener=Mock()
        client.opener.open.return_value=io.BytesIO(b'<!DOCTYPE html><html>private proxy token</html>')
        with self.assertRaisesRegex(q.RequestError,'HTML login/proxy') as caught:client.call('auth/login')
        self.assertNotIn('private proxy token',str(caught.exception))

    def test_wrong_login_is_explicit(self):
        client=q.Client('https://example.test')
        with patch.object(client,'call',return_value='Fails.'),self.assertRaisesRegex(q.RequestError,'credentials'):
            client.login('admin','not-in-error')

    def test_saved_connection_failure_can_retry_manual_url_without_changing_radarr(self):
        saved=[('AppBox','http://saved-host:123','old-user','old-secret')]
        source={'schema':1,'kind':'qbit-settings-only','preferences':{},'categories':{}}
        old=Mock();old.login.side_effect=q.RequestError('qBittorrent auth/login: HTTP 403.')
        new=Mock()
        with patch.object(q,'saved_connections',return_value=saved),patch.object(q,'Client',side_effect=[old,new]) as factory,\
             patch.object(q,'capture',return_value=source),patch('builtins.input',side_effect=['y','https://example.test','admin']),\
             patch.object(q.getpass,'getpass',return_value='new-private-secret'):
            self.assertEqual(q.capture_live(),source)
        self.assertEqual(factory.call_args.args[0],'https://example.test')
        new.login.assert_called_once_with('admin','new-private-secret')

    def test_explicit_backup_url_bypasses_saved_connection(self):
        with patch.object(q,'saved_connections') as saved,patch.object(q,'Client') as factory,\
             patch.object(q,'capture',return_value={}),patch('builtins.input',return_value='admin'),\
             patch.object(q.getpass,'getpass',return_value='hidden'):
            q.capture_live('https://example.test')
            saved.assert_not_called();factory.assert_called_once_with('https://example.test')

    def test_redirects_do_not_forward_login_to_another_origin(self):
        handler=q.SameOriginRedirect('https://example.test')
        request=urllib.request.Request('https://example.test/api/v2/auth/login',data=b'password=private')
        with self.assertRaisesRegex(q.RequestError,'another address'):
            handler.redirect_request(request,None,302,'',{},'https://other.test/login?private-token')


if __name__=='__main__':unittest.main()
