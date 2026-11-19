"""Check the actual unattended storage credentials without processing jobsheets."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

from . import asana_index, config, rclone_helper


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--write-probe',action='store_true')
    args=parser.parse_args()
    result={'input_queue_pdfs':0,'split_queue_pdfs':0,'index_verified':False,
            'onedrive_destination_available':False,'probe_requested':args.write_probe,
            'jobsheet_uploads':0}
    probes=[]
    try:
        result['input_queue_pdfs']=len(rclone_helper.list_pdfs(config.GDRIVE_INPUT))
        result['split_queue_pdfs']=len(rclone_helper.list_pdfs(config.GDRIVE_SPLIT))
        with tempfile.TemporaryDirectory(prefix='jobsheet_drive_readiness_') as folder:
            work=Path(folder)
            index=work/'index.json'; manifest=work/'manifest.json'
            rclone_helper.download(config.GDRIVE_ASANA_INDEX_JSON,index)
            rclone_helper.download(config.GDRIVE_ASANA_INDEX_MANIFEST,manifest)
            asana_index.load_index(index,manifest)
            result['index_verified']=True
            destination=rclone_helper.remote_stat(config.ONEDRIVE_OUTPUT)
            if not destination or not destination.get('IsDir'):
                raise RuntimeError('OneDrive destination is unavailable')
            result['onedrive_destination_available']=True
            if args.write_probe:
                nonce=uuid.uuid4().hex
                original=f'{config.GDRIVE_CONTROL}/.connectivity-probe-{nonce}.json'
                moved=f'{config.GDRIVE_CONTROL}/.connectivity-probe-{nonce}.moved.json'
                local=work/'probe.json'
                content=json.dumps({'purpose':'jobsheet-drive-connectivity','nonce':nonce}).encode()
                local.write_bytes(content)
                probes.extend([original,moved])
                rclone_helper.upload(local,original)
                rclone_helper.moveto(original,moved)
                downloaded=work/'probe-readback.json'
                rclone_helper.download(moved,downloaded)
                if hashlib.sha256(downloaded.read_bytes()).digest()!=hashlib.sha256(content).digest():
                    raise RuntimeError('Connectivity probe readback mismatch')
                rclone_helper.delete(moved)
                probes.clear()
                result['isolated_drive_create_move_read_delete_verified']=True
        print(json.dumps(result),flush=True)
        return 0
    except Exception as exc:
        result['error_type']=type(exc).__name__
        result['probe_cleanup_pending']=bool(probes)
        print(json.dumps(result),flush=True)
        return 1
    finally:
        # Only random test objects created above are eligible for cleanup.
        for remote in probes:
            try:
                if rclone_helper.remote_stat(remote) is not None:
                    rclone_helper.delete(remote)
            except rclone_helper.RcloneError:
                print('Drive connectivity probe needs cleanup; no jobsheet was processed',flush=True)


if __name__=='__main__':
    raise SystemExit(main())
