"""Exercise archive staging and inspection boundaries using malicious media."""
import io
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PATCH = '12345678'
FILES = {f'{PATCH}/etc/config/inventory.xml': b'<patch patchID="12345678"/>',
         f'{PATCH}/etc/config/actions.xml': b'<actions/>', f'{PATCH}/files/payload': b'payload'}


class ArtifactSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.destination = self.base / 'stage' / PATCH
        self.destination.parent.mkdir()
        self.sentinel = self.base / 'outside'
        self.sentinel.write_text('unchanged')

    def stage(self, extra=None, kind='tar', env=None):
        source = self.base / f'media.{kind}'
        if kind == 'tar':
            with tarfile.open(source, 'w') as archive:
                for name, data in FILES.items():
                    info = tarfile.TarInfo(name); info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                if extra:
                    archive.addfile(extra, io.BytesIO(b'x') if extra.isfile() else None)
        else:
            with zipfile.ZipFile(source, 'w') as archive:
                for name, data in FILES.items():
                    archive.writestr(name, data)
                if extra:
                    archive.writestr(extra, b'../outside')
        argv = [str(ROOT / 'bin/opu-artifact-stage'), '--artifact', str(self.destination),
                '--owner', pwd.getpwuid(os.getuid()).pw_name]
        if kind == 'tar':
            argv += ['--from-tar-stdin']
            data = source.read_bytes()
        else:
            argv += ['--from-zip', str(source)]
            data = None
        return subprocess.run(argv, input=data, capture_output=True, timeout=30,
                              env={**os.environ, **(env or {})})

    def assert_rejected(self, proc):
        self.assertNotEqual(proc.returncode, 0, proc.stdout.decode())
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.sentinel.read_text(), 'unchanged')

    def test_regular_tar_and_zip_stage(self):
        for kind in ('tar', 'zip'):
            with self.subTest(kind=kind):
                self.destination = self.base / f'stage-{kind}' / PATCH
                self.destination.parent.mkdir()
                result = self.stage(kind=kind)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual((self.destination / 'files/payload').read_bytes(), b'payload')

    def test_tar_links_specials_traversal_and_duplicates_are_rejected(self):
        for name, member_type in ((f'{PATCH}/files/link', tarfile.SYMTYPE),
                                  (f'{PATCH}/files/hard', tarfile.LNKTYPE),
                                  (f'{PATCH}/files/pipe', tarfile.FIFOTYPE),
                                  (f'{PATCH}/../outside', tarfile.REGTYPE),
                                  (str(self.sentinel), tarfile.REGTYPE),
                                  (f'{PATCH}/files/payload', tarfile.REGTYPE)):
            with self.subTest(name=name):
                info = tarfile.TarInfo(name); info.type = member_type
                info.linkname = str(self.sentinel)
                info.size = 1 if info.isfile() else 0
                self.assert_rejected(self.stage(info))

    def test_zip_link_and_traversal_are_rejected(self):
        link = zipfile.ZipInfo(f'{PATCH}/files/link'); link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.assert_rejected(self.stage(link, 'zip'))
        self.assert_rejected(self.stage(f'{PATCH}/../outside', 'zip'))

    def test_archive_limits_apply_before_publish(self):
        self.assert_rejected(self.stage(env={'OPU_ARTIFACT_MAX_BYTES': '1'}))
        self.assert_rejected(self.stage(env={'OPU_ARTIFACT_MAX_ENTRIES': '1'}))

    def test_inspector_rejects_link_added_after_staging(self):
        staged = self.stage()
        self.assertEqual(staged.returncode, 0, staged.stderr.decode())
        (self.destination / 'files/external').symlink_to(self.sentinel)
        proc = subprocess.run([str(ROOT / 'bin/opu-artifact-inspect'), '--artifact', str(self.destination)],
                              capture_output=True, text=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('link', proc.stderr.lower())

    def test_inspector_rejects_hardlink_added_after_staging(self):
        staged = self.stage()
        self.assertEqual(staged.returncode, 0, staged.stderr.decode())
        os.link(self.sentinel, self.destination / 'files/external')
        proc = subprocess.run([str(ROOT / 'bin/opu-artifact-inspect'), '--artifact', str(self.destination)],
                              capture_output=True, text=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)


if __name__ == '__main__':
    unittest.main()
