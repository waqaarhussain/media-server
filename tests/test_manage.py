import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
import subprocess
import shutil
import socket

spec = importlib.util.spec_from_file_location('manage', Path(__file__).resolve().parents[1] / 'scripts/manage.py')
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class MigrationTests(unittest.TestCase):
    def test_encrypted_archive_roundtrip_and_wrong_password_rejected(self):
        if not shutil.which('gpg'):
            self.skipTest('GnuPG is not installed')
        try:
            probe=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.close()
        except PermissionError:
            self.skipTest('This test runtime blocks Unix sockets required by gpg-agent')
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'config.txt'
            source.write_text('private-test-settings')
            output=root/'backup.gpg'
            base=['gpg','--homedir',str(root),'--batch','--yes','--pinentry-mode','loopback','--passphrase-fd','0']
            subprocess.run(base+['--symmetric','--cipher-algo','AES256','--output',str(output),str(source)],
                           input=b'test-only-passphrase\n',capture_output=True,check=True)
            result=subprocess.run(base+['--decrypt',str(output)],input=b'test-only-passphrase\n',capture_output=True,check=True)
            self.assertEqual(result.stdout,source.read_bytes())
            self.assertNotIn(source.read_bytes(),output.read_bytes())
            failed=subprocess.run(base+['--no-symkey-cache','--decrypt',str(output)],input=b'wrong-passphrase\n',capture_output=True)
            self.assertNotEqual(failed.returncode,0)

    def test_mapping_respects_folder_boundaries_and_nested_roots(self):
        pairs=[('/media/Movies', '/new/Movies'), ('/media/Movies/Kids', '/new/Kids')]
        self.assertEqual(manage.remap_path('/media/Movies/Film/a.mkv', pairs), '/new/Movies/Film/a.mkv')
        self.assertEqual(manage.remap_path('/media/Movies/Kids/Film', pairs), '/new/Kids/Film')
        self.assertEqual(manage.remap_path('/media/Movies-old/Film', pairs), '/media/Movies-old/Film')
        self.assertEqual(manage.remap_path(123, pairs), 123)

    def test_archive_rejects_traversal_and_links_before_extracting_any_file(self):
        for dangerous, kind in (('../outside', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)):
            with self.subTest(name=dangerous), tempfile.TemporaryDirectory() as folder:
                buffer=io.BytesIO()
                with tarfile.open(fileobj=buffer, mode='w') as tar:
                    safe=tarfile.TarInfo('safe.txt')
                    safe.size=2
                    tar.addfile(safe, io.BytesIO(b'ok'))
                    bad=tarfile.TarInfo(dangerous)
                    bad.type=kind
                    bad.linkname='/etc/passwd'
                    tar.addfile(bad)
                buffer.seek(0)
                with tarfile.open(fileobj=buffer) as tar:
                    with self.assertRaises(ValueError):
                        manage.safe_extract(tar, Path(folder)/'unpack')
                self.assertFalse((Path(folder)/'unpack/safe.txt').exists())

    def test_backup_copies_settings_but_never_movie_payloads_or_linked_media(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            source=root/'source'
            source.mkdir()
            (source/'config.xml').write_text('settings')
            (source/'Film.MKV').write_text('movie')
            (source/'job.torrent').write_text('torrent')
            external=root/'media'
            external.mkdir()
            (external/'Film.mkv').write_text('movie')
            (source/'mounted').symlink_to(external, target_is_directory=True)
            manage.snapshot_copy(source, root/'backup')
            self.assertEqual((root/'backup/config.xml').read_text(),'settings')
            self.assertFalse((root/'backup/Film.MKV').exists())
            self.assertFalse((root/'backup/job.torrent').exists())
            self.assertFalse((root/'backup/mounted/Film.mkv').exists())
            self.assertTrue((root/'backup/mounted.symlink-reference.txt').exists())


if __name__ == '__main__':
    unittest.main()
