"""Read-only validation policy, independent of the pinned production checkout."""
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse


class ReadOnlyGuard:
    def __init__(self, counters, *, vision_host, download_hosts=(), popen=None):
        self.counters = counters
        self.vision_host = vision_host
        self.download_hosts = set(download_hosts)
        self._popen = popen or subprocess.Popen
        self._uname = shutil.which('uname')
        self._rclone = shutil.which('rclone')
        self._downloads = set()
        for key in ('cloud_write_attempts', 'blocked_network_reads',
                    'blocked_subprocess_attempts', 'local_metadata_queries'):
            counters.setdefault(key, 0)

    def allow_drive_download(self, remote, destination, *options):
        if (not str(remote).startswith('googledrive:')
                or not Path(destination).is_absolute()
                or '..' in str(remote).split('/')
                or (options and not (len(options)==3
                    and options[0]=='--drive-root-folder-id'
                    and options[2]=='--drive-trashed-only'))):
            raise ValueError('Only explicit Drive-to-local downloads can be registered')
        self._downloads.add(tuple(['rclone','copyto',str(remote),str(destination),*options]))

    def request_allowed(self, method, url):
        parsed = urlparse(str(url))
        if parsed.scheme != 'https':
            return False
        method = method.upper()
        return ((parsed.hostname == 'app.asana.com' and method == 'GET') or
                (parsed.hostname in self.download_hosts and method == 'GET') or
                (parsed.hostname == self.vision_host and method == 'POST'))

    def reject_cloud_write(self, *args, **kwargs):
        self.counters['cloud_write_attempts'] += 1
        raise RuntimeError('Cloud mutation blocked by read-only guard')

    def reject_network(self, method):
        if method.upper() not in ('GET', 'HEAD', 'OPTIONS'):
            return self.reject_cloud_write()
        self.counters['blocked_network_reads'] += 1
        raise RuntimeError('Unexpected network read blocked')

    def reject_subprocess(self, *args, **kwargs):
        self.counters['blocked_subprocess_attempts'] += 1
        raise RuntimeError('Unexpected subprocess blocked')

    def popen(self, args, *pos, **kwargs):
        # Python/OpenAI on Linux asks only for CPU metadata. Pin the executable
        # resolved before installing the guard; never allow a shell, arbitrary
        # arguments or an executable/environment override through this route.
        if (self._uname and isinstance(args, (list, tuple))
                and list(args) == ['uname', '-p'] and not pos
                and not any(kwargs.get(k) for k in
                            ('shell', 'executable', 'env', 'cwd', 'preexec_fn', 'pass_fds'))):
            self.counters['local_metadata_queries'] += 1
            return self._popen([self._uname, '-p'], **kwargs)
        if (self._rclone and isinstance(args,(list,tuple))
                and tuple(str(a) for a in args) in self._downloads and not pos
                and not any(kwargs.get(k) for k in
                            ('shell','executable','env','cwd','preexec_fn','pass_fds'))):
            self.counters['input_downloads']=self.counters.get('input_downloads',0)+1
            return self._popen([self._rclone,*args[1:]],**kwargs)
        return self.reject_subprocess()

    def assert_clean(self):
        denied = {key:self.counters[key] for key in (
            'cloud_write_attempts', 'blocked_network_reads', 'blocked_subprocess_attempts')}
        if any(denied.values()):
            raise RuntimeError(f'Read-only guard rejected unexpected operations: {denied}')
