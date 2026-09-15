#!/usr/bin/env python3
"""Exercise streamed server inventory with isolated command/interpreter fixtures."""
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='opu-inventory-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / 'commands'
        self.bin.mkdir()
        for command in ('hostname', 'uname', 'id', 'getconf', 'df', 'awk'):
            self.executable(command, '''#!/bin/sh
case "${0##*/}" in
  hostname) printf 'inventory-fixture\\n' ;;
  uname) printf 'Linux fixture x86_64\\n' ;;
  id) case "$1" in -un) printf 'fixture\\n';; -u) printf '1000\\n';; *) exit 1;; esac ;;
  getconf) printf '2\\n' ;;
  df) printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n' ;;
  awk) exit 0 ;;
esac
''')
        self.wrapper = self.base / 'interpreter.py'
        self.wrapper.write_text('''import importlib.util
import sys
major, minor = (int(value) for value in sys.argv[1].split('.'))
sys.version_info = (major, minor, 0)
sys.version = sys.argv[1] + '.0 simulated'
missing = sys.argv[2]
original = importlib.util.find_spec
importlib.util.find_spec = lambda name: None if name == missing else original(name)
exec(compile(sys.stdin.read(), '<inventory-script>', 'exec'), {'__name__': '__main__'})
''')

    def executable(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)
        return path

    def interpreter(self, name, version, missing='none'):
        args = [sys.executable, str(self.wrapper), version, missing]
        return self.executable(name, '#!/bin/sh\nexec ' + shlex.join(args) + '\n')

    def inventory(self):
        result = subprocess.run(['/bin/bash', '-s'],
                                input=(ROOT / 'deploy/server-preflight.sh').read_text(),
                                env={'PATH': str(self.bin), 'LANG': 'C', 'LC_ALL': 'C'},
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('READ_ONLY_COMPLETE:', result.stdout)
        self.assertFalse(result.stderr, result.stderr)
        return result.stdout

    def test_supported_versioned_python_is_found_without_changing_old_default(self):
        old = self.interpreter('python3', '3.6')
        selected = self.interpreter('python3.12', '3.12')
        before = old.read_bytes()
        output = self.inventory()
        self.assertIn('PYTHON_SELECTED ' + str(selected), output)
        self.assertNotIn('PYTHON_BLOCKER', output)
        self.assertEqual(old.read_bytes(), before)

    def test_python_39_or_missing_python_reports_blocker_but_completes_inventory(self):
        self.interpreter('python3', '3.9')
        output = self.inventory()
        self.assertIn('PYTHON_BLOCKER: controller dependencies require Python 3.10+', output)
        self.assertNotIn('PYTHON_SELECTED', output)
        (self.bin / 'python3').unlink()
        self.assertIn('PYTHON_BLOCKER:', self.inventory())

    def test_incomplete_newer_interpreter_does_not_hide_usable_interpreter(self):
        self.interpreter('python3.14', '3.14', missing='ensurepip')
        self.interpreter('python3.13', '3.13', missing='venv')
        selected = self.interpreter('python3.12', '3.12')
        output = self.inventory()
        self.assertIn('PYTHON_ENSUREPIP False', output)
        self.assertIn('PYTHON_VENV False', output)
        self.assertIn('PYTHON_SELECTED ' + str(selected), output)

    def test_supported_unversioned_interpreter_is_accepted(self):
        selected = self.interpreter('python3', '3.10')
        self.assertIn('PYTHON_SELECTED ' + str(selected), self.inventory())


if __name__ == '__main__':
    unittest.main()
