import contextlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import Mock,patch

from test_manage import manage
import qbit_migrate as q


DOCUMENT={'schema':1,'kind':'qbit-settings-only','version':'v4.6.3',
          'preferences':{'up_limit':123},'categories':{'Kids Movies':{'savePath':'/old/kids'}}}


class CombinedBackupTests(unittest.TestCase):
    def test_main_archive_includes_qbit_settings_and_captures_before_services_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);home=root/'home';home.mkdir()
            for app,(binary,_,_) in manage.APPS.items():
                data=root/'var/lib'/app;data.mkdir(parents=True)
                (data/'config.xml').write_text('<Config/>')
                with sqlite3.connect(data/(app+'.db')) as db:db.execute('CREATE TABLE Settings(value TEXT)')
                folder=root/'opt'/binary;folder.mkdir(parents=True);(folder/binary).write_text('binary fixture')
            for name in ('.config','.local/state','.local/share'):
                (home/name/'media-request-bot').mkdir(parents=True)
            events=[];captured={}
            def routed_path(value):
                path=Path(value)
                if path.is_absolute() and str(path).startswith(('/var/lib','/opt','/etc','/usr/local/bin')):
                    return root/str(path).lstrip('/')
                return path
            real_copy=manage.snapshot_copy
            def snapshot_copy(source,dest,*args,**kwargs):
                if source==manage.REPO:
                    dest.mkdir(parents=True);return
                return real_copy(source,dest,*args,**kwargs)
            def run(args,**kwargs):
                if args[0]=='systemctl':events.append(args[1]+':'+args[2])
                if args[0]=='gpg':
                    with tarfile.open(args[-1]) as archive:
                        captured['names']=archive.getnames()
                        captured['qbit']=json.load(archive.extractfile('snapshot/qbit-settings.json'))
                        captured['manifest']=json.load(archive.extractfile('snapshot/manifest.json'))
                    Path(args[args.index('--output')+1]).write_bytes(b'fake ciphertext for archive structure test')
            def capture():events.append('capture-qbit');return DOCUMENT
            output=io.StringIO()
            with patch.object(manage,'Path',side_effect=routed_path),patch.object(manage,'own'),\
                 patch.object(manage,'snapshot_copy',side_effect=snapshot_copy),patch.object(manage,'run',side_effect=run),\
                 patch.object(manage,'passphrase',return_value=b'test\n'),patch.object(manage,'revision',return_value='test-revision'),\
                 patch.object(manage,'api',return_value={'version':'fixture'}),\
                 patch.object(manage,'active',side_effect=lambda s:s=='radarr.service'),\
                 patch.object(manage.shutil,'which',return_value=None),patch.object(q,'capture_live',side_effect=capture),\
                 contextlib.redirect_stdout(output):
                destination=home/'unified.tar.gz.gpg'
                manage.backup({'home':str(home),'user':'test'},destination)
            self.assertEqual(captured['qbit'],DOCUMENT)
            self.assertEqual(captured['manifest']['qbit_settings']['version'],'v4.6.3')
            self.assertLess(events.index('capture-qbit'),events.index('stop:radarr.service'))
            self.assertIn('start:radarr.service',events)
            self.assertFalse(any(name.endswith(('.torrent','.fastresume','.mkv')) for name in captured['names']))
            self.assertEqual(list(home.glob('*.gpg')),[destination])
            self.assertFalse(list(home.glob('*.partial')))
            self.assertIn('qBittorrent preferences and categories included',output.getvalue())

    def test_unavailable_qbit_aborts_without_stopping_services_or_claiming_success(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(manage,'own'),\
             patch.object(q,'capture_live',side_effect=ValueError('qBittorrent unavailable')),\
             patch.object(manage,'run') as run,patch.object(manage,'passphrase') as secret:
            with self.assertRaisesRegex(ValueError,'unavailable'):
                manage.backup({'home':tmp,'user':'test'})
            run.assert_not_called();secret.assert_not_called()
            self.assertFalse(list(Path(tmp).rglob('*.gpg')))

    def test_multiple_categories_on_same_client_need_no_additional_connection_prompt(self):
        choices=[('Movies','http://host:8080','user','password'),('Kids','http://host:8080','user','password')]
        with patch.object(q,'saved_connections',return_value=choices),patch.object(q,'Client') as client,\
             patch.object(q,'capture',return_value=DOCUMENT),patch('builtins.input') as prompt:
            self.assertEqual(q.capture_live(),DOCUMENT)
            prompt.assert_not_called()
            client.return_value.login.assert_called_once_with('user','password')


class BackupCommandTests(unittest.TestCase):
    def test_backup_url_override_is_passed_to_combined_backup(self):
        cfg={'home':'/fixture/home','user':'fixture'}
        with patch.object(manage.sys,'argv',['manage.py','backup','--qbit-url','https://example.test']), \
             patch.object(manage.os,'geteuid',return_value=0),patch.object(manage,'account',return_value=cfg), \
             patch.object(manage,'backup') as backup:
            manage.main()
            backup.assert_called_once_with(cfg,None,qbit_url='https://example.test')


if __name__=='__main__':unittest.main()
