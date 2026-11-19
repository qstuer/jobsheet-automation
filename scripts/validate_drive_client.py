"""Fail clearly before queue processing if Drive still uses retired shared OAuth."""
import argparse
import configparser
import json
from pathlib import Path

SHARED_PROJECT = '202264815644'


def validate(path):
    settings = configparser.ConfigParser(interpolation=None)
    if not settings.read(path, encoding='utf-8') or 'googledrive' not in settings:
        raise ValueError('Missing googledrive configuration')
    drive = settings['googledrive']
    client = drive.get('client_id','').strip()
    if not client or client.split('-')[0] == SHARED_PROJECT:
        raise ValueError('Drive still uses the shared rclone client; configure the dedicated Jobsheet OAuth client')
    if not drive.get('client_secret','').strip():
        raise ValueError('Dedicated Drive client secret is missing')
    try:
        refresh = bool(json.loads(drive.get('token','{}')).get('refresh_token'))
    except (ValueError,TypeError):
        refresh = False
    if not refresh:
        raise ValueError('Drive offline refresh token is missing; authorize the dedicated client')
    if drive.get('scope','').strip() != 'drive':
        raise ValueError('Drive scope differs from the existing approved pipeline scope')
    return {'dedicated_drive_client':True,'offline_refresh_available':True,'scope':'drive'}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--config',type=Path,default=Path.home()/'.config/rclone/rclone.conf')
    args=parser.parse_args()
    try:
        print(json.dumps(validate(args.config)))
    except ValueError as exc:
        print(f'Drive configuration check failed: {exc}')
        return 1
    return 0


if __name__=='__main__':
    raise SystemExit(main())
