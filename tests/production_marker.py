"""Production authority-file admission through controller and native entrypoints.

All files are disposable; no Oracle or network operations run here.
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'webapp'))
import production


class ProductionMarker(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cert = self.root / 'production.cert'
        self.cert.write_text('\n'.join(production.CHECKLIST_KEYS) + '\n')
        self.cert.chmod(0o600)
        self.env = {'OPU_PRODUCTION_MODE': '1', 'OPU_PRODUCTION_REQUIRE_CHECKLIST': '1',
                    'OPU_PRODUCTION_CERT_FILE': str(self.cert)}
        self.enterContext(patch.dict(os.environ, self.env))

    def check_gates(self, accepted):
        self.assertEqual(production.is_certified(), accepted, 'controller marker gate')
        result = subprocess.run(['bash', str(ROOT / 'scripts/verify_production_cert.sh')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_private_marker_is_accepted(self):
        self.check_gates(True)

    def test_shared_writable_markers_are_not_authority(self):
        for mode in (0o620, 0o602, 0o666):
            with self.subTest(mode=oct(mode)):
                self.cert.chmod(mode)
                self.check_gates(False)

    def test_hard_link_and_symlink_are_not_authority(self):
        second = self.root / 'alias'
        second.hardlink_to(self.cert)
        self.check_gates(False)
        second.unlink()
        self.cert.rename(second)
        self.cert.symlink_to(second)
        self.check_gates(False)

    def test_special_and_oversized_files_fail_promptly(self):
        self.cert.unlink()
        os.mkfifo(self.cert, 0o600)
        # A FIFO must never reach a blocking open/read, even in the controller.
        code = 'import sys; sys.path.insert(0, sys.argv[1]); import production; print(production.is_certified())'
        result = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT / 'webapp')],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.stdout.strip(), 'False', result.stderr)
        self.check_gates(False)
        self.cert.unlink()
        self.cert.write_text('\n'.join(production.CHECKLIST_KEYS) + '\n' + '#' * 65536)
        self.cert.chmod(0o600)
        self.check_gates(False)

    def test_conflicting_and_lookalike_markers_fail_closed(self):
        for extra in ('OPU_PRODUCTION_CERTIFIED=0', 'OPU_PRODUCTION_CERTIFIED=1'):
            with self.subTest(extra=extra):
                self.cert.write_text('\n'.join(production.CHECKLIST_KEYS) + '\n' + extra + '\n')
                self.check_gates(False)
        self.cert.write_text('\n'.join(production.CHECKLIST_KEYS).replace('CERTIFIED=1', 'CERTIFIED=10'))
        self.check_gates(False)

    def test_invalid_encoding_is_rejected(self):
        self.cert.write_bytes(self.cert.read_bytes() + b'\xff')
        self.check_gates(False)

    def test_untrusted_owner_is_rejected(self):
        if os.geteuid() == 0:
            os.chown(self.cert, 65534, -1)
            self.assertFalse(production.is_certified())
        else:
            with patch.object(production._marker.os, 'geteuid', return_value=os.geteuid() + 1):
                self.assertFalse(production.is_certified())

    def test_read_rejects_file_mutation_or_name_replacement(self):
        for change in ('content', 'permissions', 'replacement'):
            with self.subTest(change=change):
                self.cert.write_text('\n'.join(production.CHECKLIST_KEYS) + '\n')
                self.cert.chmod(0o600)
                original = os.fstat
                count = 0
                def raced_fstat(fd):
                    nonlocal count
                    count += 1
                    if count == 2:
                        if change == 'content':
                            self.cert.write_text('OPU_PRODUCTION_CERTIFIED=0\n')
                        elif change == 'permissions':
                            self.cert.chmod(0o666)
                        else:
                            self.cert.rename(self.root / 'retired.cert')
                            self.cert.write_text('\n'.join(production.CHECKLIST_KEYS) + '\n')
                            self.cert.chmod(0o600)
                    return original(fd)
                with patch.object(production._marker.os, 'fstat', side_effect=raced_fstat):
                    self.assertFalse(production.is_certified())

    def test_status_uses_one_marker_observation(self):
        valid = '\n'.join(production.CHECKLIST_KEYS) + '\n'
        with patch.object(production, '_cert_text', side_effect=[valid, '']) as read:
            state = production.status()
        self.assertTrue(state['certified'])
        self.assertTrue(all(state['checklist'].values()))
        self.assertEqual(read.call_count, 1)


if __name__ == '__main__':
    unittest.main()
