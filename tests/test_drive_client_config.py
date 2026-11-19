import json
from pathlib import Path
import tempfile
import unittest

from scripts.validate_drive_client import validate


class DriveClientConfigurationTests(unittest.TestCase):
    def check_config(self,client='12345-test.apps.googleusercontent.com',secret='test-only',token=None,scope='drive'):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'rclone.conf'
            path.write_text('[googledrive]\ntype = drive\nclient_id = '+client+
                            '\nclient_secret = '+secret+'\nscope = '+scope+
                            '\ntoken = '+(token if token is not None else json.dumps({'refresh_token':'test-only'}))+'\n',encoding='utf-8')
            return validate(path)

    def test_dedicated_offline_client_passes_without_exposing_values(self):
        result=self.check_config()
        self.assertEqual(result,{'dedicated_drive_client':True,'offline_refresh_available':True,'scope':'drive'})

    def test_empty_or_explicit_shared_client_is_rejected(self):
        for value in ('','202264815644-test.apps.googleusercontent.com'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError,'shared rclone'):
                self.check_config(client=value)

    def test_incomplete_or_changed_authorization_is_rejected(self):
        for kwargs in ({'secret':''},{'token':'{}'},{'token':'invalid-json'},{'scope':'drive.readonly'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.check_config(**kwargs)


if __name__=='__main__':
    unittest.main()
