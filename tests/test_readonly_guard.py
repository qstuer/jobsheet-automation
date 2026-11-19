import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.readonly_guard import ReadOnlyGuard


class ReadOnlyGuardTests(unittest.TestCase):
    def setUp(self):
        self.counts = {}
        self.native = Mock(return_value='local-query')
        with patch('scripts.readonly_guard.shutil.which', return_value='/usr/bin/uname'):
            self.guard = ReadOnlyGuard(self.counts, vision_host='vision.example',
                                      download_hosts={'download.example'}, popen=self.native)

    def test_local_sdk_metadata_does_not_become_a_cloud_write(self):
        self.assertEqual(self.guard.popen(['uname','-p'], stdout=-1, text=True), 'local-query')
        self.native.assert_called_once_with(['/usr/bin/uname','-p'], stdout=-1, text=True)
        self.assertEqual(self.counts['local_metadata_queries'],1)
        self.assertEqual(self.counts['cloud_write_attempts'],0)
        self.guard.assert_clean()

    def test_only_registered_drive_to_local_download_is_allowed(self):
        self.guard._rclone='/usr/bin/rclone'
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory).resolve()/'input.pdf'
            self.guard.allow_drive_download('googledrive:allowed.pdf',target)
            command=['rclone','copyto','googledrive:allowed.pdf',str(target)]
            self.guard.popen(command,stdout=-1,text=True)
            self.native.assert_called_once_with(['/usr/bin/rclone',*command[1:]],stdout=-1,text=True)
            self.guard.assert_clean()
            with self.assertRaises(RuntimeError):
                self.guard.popen(['rclone','copyto','googledrive:other.pdf',str(target)])
        self.assertEqual(self.counts['input_downloads'],1)
        self.assertEqual(self.counts['cloud_write_attempts'],0)

    def test_cloud_destination_and_arbitrary_options_cannot_be_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            for remote,target,options in (
                ('googledrive:source.pdf','onedrive:destination.pdf',()),
                ('onedrive:source.pdf',str(Path(directory)/'out.pdf'),()),
                ('googledrive:source.pdf',str(Path(directory)/'out.pdf'),('--config','other.conf'))):
                with self.subTest(remote=remote),self.assertRaises(ValueError):
                    self.guard.allow_drive_download(remote,target,*options)

    def test_other_subprocesses_and_overrides_are_blocked(self):
        for args,kwargs in ((['uname','-a'],{}),(['uname','-p'],{'shell':True}),
                            (['uname','-p'],{'executable':'rclone'}),
                            (['rclone','copyto','local.pdf','onedrive:'],{})):
            with self.subTest(args=args,kwargs=kwargs), self.assertRaises(RuntimeError):
                self.guard.popen(args,**kwargs)
        self.native.assert_not_called()
        self.assertEqual(self.counts['cloud_write_attempts'],0)
        self.assertEqual(self.counts['blocked_subprocess_attempts'],4)
        with self.assertRaises(RuntimeError):
            self.guard.assert_clean()

    def test_remote_write_is_still_rejected_and_fails_acceptance(self):
        for method,url in (('PUT','https://app.asana.com/api/1.0/tasks/x'),
                           ('POST','https://www.googleapis.com/drive/v3/files'),
                           ('PUT','https://graph.microsoft.com/v1.0/drives/x')):
            self.assertFalse(self.guard.request_allowed(method,url))
            with self.assertRaises(RuntimeError):
                self.guard.reject_network(method)
        self.assertEqual(self.counts['cloud_write_attempts'],3)
        with self.assertRaises(RuntimeError):
            self.guard.assert_clean()

    def test_only_prescribed_https_reads_and_vision_posts_are_allowed(self):
        for method,url in (('GET','https://app.asana.com/api/1.0/tasks/x'),
                           ('GET','https://download.example/input'),
                           ('POST','https://vision.example/v1/chat/completions')):
            self.assertTrue(self.guard.request_allowed(method,url))
        self.assertFalse(self.guard.request_allowed('GET','http://app.asana.com/api/1.0/tasks/x'))
        self.assertFalse(self.guard.request_allowed('POST','https://download.example/input'))
        self.assertFalse(self.guard.request_allowed('GET','https://other.example/'))
        with self.assertRaises(RuntimeError):
            self.guard.reject_network('GET')
        self.assertEqual(self.counts['cloud_write_attempts'],0)
        self.assertEqual(self.counts['blocked_network_reads'],1)


if __name__=='__main__':
    unittest.main()
