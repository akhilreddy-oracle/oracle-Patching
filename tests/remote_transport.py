#!/usr/bin/env python3
"""Offline stream/process and ephemeral SSH trust regressions."""
import base64
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "webapp"))
import remote


def completed(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class RemoteTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_stream_drains_large_source_stderr_without_blocking_the_consumer(self):
        commands = {
            "source": [sys.executable, "-c", "import sys; sys.stderr.write('diagnostic'*30000); sys.stdout.write('payload')"],
            "destination": [sys.executable, "-c", "import sys; print('received:' + sys.stdin.read())"],
        }
        with patch.object(remote, "_ssh_argv", side_effect=lambda alias, *args: commands[alias]):
            result = remote.pipe_remote("source", ["producer"], "destination", ["consumer"], timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "received:payload")
        self.assertIn("diagnostic", result.stderr)
        self.assertLess(len(result.stderr), 66000)

    def test_stream_source_failure_cannot_be_hidden_by_successful_consumer(self):
        commands = {
            "source": [sys.executable, "-c", "import sys; print('partial'); sys.exit(7)"],
            "destination": [sys.executable, "-c", "import sys; sys.stdin.read()"],
        }
        with patch.object(remote, "_ssh_argv", side_effect=lambda alias, *args: commands[alias]):
            result = remote.pipe_remote("source", ["producer"], "destination", ["consumer"], timeout=5)
        self.assertEqual(result.returncode, 7)

    def test_stream_reaps_both_processes_after_timeout_or_destination_spawn_failure(self):
        actual_popen = subprocess.Popen
        for failure in ("timeout", "spawn"):
            with self.subTest(failure=failure):
                children = []
                def spawn(*args, **kwargs):
                    if failure == "spawn" and children:
                        raise OSError("fixture could not spawn")
                    child = actual_popen(*args, **kwargs)
                    children.append(child)
                    return child
                with patch.object(remote, "_ssh_argv", return_value=[sys.executable, "-c", "import time; time.sleep(60)"]), \
                     patch.object(remote.subprocess, "Popen", side_effect=spawn):
                    with self.assertRaises(remote.RemoteError) as caught:
                        remote.pipe_remote("source", ["producer"], "destination", ["consumer"], timeout=0.1)
                self.assertEqual(caught.exception.error, "ssh_timeout" if failure == "timeout" else "ssh_unavailable")
                self.assertEqual(len(children), 2 if failure == "timeout" else 1)
                self.assertTrue(all(child.poll() is not None for child in children))
                self.assertTrue(all(child.stdout is None or child.stdout.closed for child in children[:1]))

    def test_ip_and_port_inputs_are_validated_before_remote_calls(self):
        with patch.object(remote, "run_remote_shell") as shell:
            for address in ("999.1.1.1", "1.2.3", "1.2.3.4; echo injected", None):
                with self.subTest(address=address), self.assertRaises(remote.RemoteError):
                    remote.reachable_from("source", [address])
            for port in (0, 65536, True, "22"):
                with self.subTest(port=port), self.assertRaises(remote.RemoteError):
                    remote.reachable_from("source", ["10.0.0.1"], port=port)
            shell.assert_not_called()
        with patch.object(remote, "run_remote_shell", return_value=completed(out="999.0.0.1 10.0.0.2 ::1 10.0.0.2\n")):
            self.assertEqual(remote.host_ips("host"), ["10.0.0.2"])
        with patch.object(remote, "run_remote_shell", return_value=completed(rc=1, out="10.0.0.2")):
            with self.assertRaises(remote.RemoteError):
                remote.host_ips("host")

    def test_ssh_spawn_errors_are_controlled_for_each_primitive(self):
        with patch.object(remote.subprocess, "run", side_effect=FileNotFoundError("missing SSH")):
            for call in (lambda: remote.run_remote_raw("host", ["true"]), lambda: remote.run_remote_shell("host", "true"),
                         lambda: remote.push_file("host", "/tmp/fixture", b"x"), lambda: remote.pull_file("host", "/tmp/fixture")):
                with self.assertRaises(remote.RemoteError) as caught:
                    call()
                self.assertEqual(caught.exception.error, "ssh_unavailable")
        with self.assertRaises(remote.RemoteError):
            remote._ssh_argv("host\x7f", "true", 10)

    def test_private_push_creates_mode_600_and_refuses_an_existing_path(self):
        path = self.root / "key"
        with patch.object(remote, "_ssh_argv", side_effect=lambda alias, command, timeout: ["bash", "-c", command]):
            remote.push_file("fixture", str(path), b"temporary key", private=True)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(remote.RemoteError):
                remote.push_file("fixture", str(path), b"replacement", private=True)
        self.assertEqual(path.read_bytes(), b"temporary key")


class DirectTransferTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.fail = None
        self.blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + b"x" * 32
        self.host_key = "ssh-ed25519 " + base64.b64encode(self.blob).decode()
        self.shell = self.enterContext(patch.object(remote, "run_remote_shell", side_effect=self.run_shell))
        self.push = self.enterContext(patch.object(remote, "push_file"))
        self.keygen = self.enterContext(patch.object(remote.subprocess, "run", side_effect=self.generate_key))

    def generate_key(self, argv, **kwargs):
        path = Path(argv[argv.index("-f") + 1])
        marker = argv[argv.index("-C") + 1]
        path.write_bytes(b"fixture private key")
        path.with_suffix(".pub").write_text(self.host_key + " " + marker)
        return completed()

    def run_shell(self, alias, script, **kwargs):
        self.calls.append((alias, script))
        if script == "id -un":
            if self.fail == "identity":
                return completed(1, "root\n")
            if self.fail == "bad_user":
                return completed(out='opc\ninjected')
            return completed(out="opc\n")
        if "/etc/ssh/ssh_host_ed25519_key.pub" in script:
            return completed(out="invalid key" if self.fail == "host_key" else self.host_key + " trusted-host\n")
        if "mktemp -d /tmp/opu-transfer." in script:
            return completed(out="/tmp/opu-transfer.ABCDEFGHIJKL\n")
        if "printf '%s\\n'" in script:
            if self.fail == "install_reply":
                raise remote.RemoteError("ssh_timeout", "reply lost after append")
            return completed()
        if "awk -v marker=" in script:
            return completed(1 if self.fail == "destination_cleanup" else 0)
        if script.startswith("set -eu; rm -f --"):
            if self.fail == "source_cleanup":
                raise remote.RemoteError("ssh_timeout", "cleanup reply lost")
            return completed()
        if script.startswith("set -o pipefail"):
            return completed(out='{"status":"staged"}')
        raise AssertionError((alias, script))

    def transfer(self, **kwargs):
        arguments = dict(src_alias="source", src_argv=["tar", "-cf", "-", "media"], dst_alias="destination",
                         dst_argv=["stage", "--path", '/oracle/path with "quotes" and \\ slash'],
                         dst_ip="10.0.0.2", src_ips=["10.0.0.1"], src_sudo=True, dst_sudo=True)
        arguments.update(kwargs)
        return remote.direct_transfer(**arguments)

    def run_authorization_script(self, script, directory):
        # Exercise the Linux stat contract with portable local fixtures. Only
        # stat formatting and flock are adapted; all file guards run in bash.
        stat_program = (
            "import os,sys; p=sys.argv[-1]; s=os.fstat(9) if p.startswith('/proc/') else os.stat(p); "
            "f=sys.argv[-2]; "
            "print(f.replace('%u',str(s.st_uid)).replace('%h',str(s.st_nlink)).replace('%d',str(s.st_dev)).replace('%i',str(s.st_ino)))"
        )
        prefix = "flock() { :; }; stat() { " + shlex.quote(sys.executable) + " -c " + shlex.quote(stat_program) + ' "$@"; }; '
        adapted = prefix + script.replace("~/.ssh", shlex.quote(str(directory)))
        process = subprocess.Popen(["bash", "-c", adapted], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate(timeout=5)
        return process.returncode, stderr

    def test_direct_transfer_pins_authenticated_keys_and_uses_private_source_storage(self):
        result = self.transfer()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.push.call_args_list), 2)
        for call in self.push.call_args_list:
            self.assertTrue(call.kwargs["private"])
            self.assertTrue(call.args[1].startswith("/tmp/opu-transfer.ABCDEFGHIJKL/"))
        self.assertEqual(self.push.call_args_list[1].args[2], ("10.0.0.2 " + self.host_key + "\n").encode())
        transfer = next(script for _, script in self.calls if script.startswith("set -o pipefail"))
        self.assertIn("StrictHostKeyChecking=yes", transfer)
        self.assertNotIn("accept-new", transfer)
        self.assertIn("GlobalKnownHostsFile=/dev/null", transfer)
        self.assertIn("opc@10.0.0.2", transfer)
        install = next(script for _, script in self.calls if "printf '%s\\n'" in script)
        for restriction in ('from="10.0.0.1"', 'command="sudo -n stage', "no-pty", "no-port-forwarding", "no-agent-forwarding"):
            self.assertIn(restriction, install)
        self.assertTrue(any("awk -v marker=" in script for _, script in self.calls))
        self.assertTrue(any(script.startswith("set -eu; rm -f --") for _, script in self.calls))

    def test_unverified_identity_or_host_keys_cannot_install_a_key(self):
        for failure in ("identity", "bad_user", "host_key"):
            with self.subTest(failure=failure):
                self.fail = failure
                self.calls.clear()
                with self.assertRaises(remote.RemoteError) as caught:
                    self.transfer()
                self.assertEqual(caught.exception.error, "transfer_setup_failed")
                self.assertFalse(any("authorized_keys" in script for _, script in self.calls))
                self.push.assert_not_called()
                self.keygen.assert_not_called()

    def test_invalid_addresses_cannot_cross_the_ssh_boundary(self):
        for kwargs in ({"dst_ip": "999.1.1.1"}, {"src_ips": ['10.0.0.1",command="bad']}, {"src_ips": []}):
            with self.subTest(kwargs=kwargs), self.assertRaises(remote.RemoteError):
                self.transfer(**kwargs)
        self.shell.assert_not_called()

    def test_lost_install_acknowledgement_still_removes_both_credentials(self):
        self.fail = "install_reply"
        with self.assertRaises(remote.RemoteError) as caught:
            self.transfer()
        self.assertEqual(caught.exception.error, "ssh_timeout")
        self.assertTrue(any("awk -v marker=" in script for _, script in self.calls))
        self.assertTrue(any(script.startswith("set -eu; rm -f --") for _, script in self.calls))
        self.assertFalse(any(script.startswith("set -o pipefail") for _, script in self.calls))

    def test_cleanup_errors_cannot_report_success_or_permit_automatic_relay(self):
        for failure in ("source_cleanup", "destination_cleanup"):
            with self.subTest(failure=failure):
                self.fail = failure
                self.calls.clear()
                with self.assertRaises(remote.RemoteError) as caught:
                    self.transfer()
                self.assertEqual(caught.exception.error, "transfer_cleanup_failed")
                self.assertIn("directory=/tmp/opu-transfer.ABCDEFGHIJKL", caught.exception.stderr)
                self.assertIn("authorization_marker=opu-transfer-", caught.exception.stderr)
                self.assertTrue(any("awk -v marker=" in script for _, script in self.calls))
                self.assertTrue(any(script.startswith("set -eu; rm -f --") for _, script in self.calls))

    def test_authorization_scripts_preserve_unrelated_keys_and_have_valid_shell_syntax(self):
        self.transfer()
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".ssh"
            directory.mkdir()
            authorized = directory / "authorized_keys"
            authorized.write_text("existing-key unrelated-operator\n")
            for _, script in self.calls:
                if "authorized_keys" not in script:
                    continue
                rc, stderr = self.run_authorization_script(script, directory)
                self.assertEqual(rc, 0, stderr)
                if "printf '%s\\n'" in script:
                    line = authorized.read_text().splitlines()[-1]
                    encoded = line.split('command="', 1)[1]
                    decoded = ""
                    escaping = False
                    for character in encoded:
                        if escaping:
                            decoded += character
                            escaping = False
                        elif character == "\\":
                            escaping = True
                        elif character == '"':
                            break
                        else:
                            decoded += character
                    self.assertEqual(shlex.split(decoded), ["sudo", "-n", "stage", "--path",
                                                           '/oracle/path with "quotes" and \\ slash'])
            self.assertEqual(authorized.read_text(), "existing-key unrelated-operator\n")
            self.assertEqual(stat.S_IMODE(authorized.stat().st_mode), 0o600)

    def test_authorization_rejects_fifo_or_linked_paths_without_mutation(self):
        self.transfer()
        install = next(script for _, script in self.calls if "printf '%s\\n'" in script)
        for filename in ("authorized_keys", "opu-transfer.lock"):
            for kind in ("fifo", "hardlink", "symlink"):
                with self.subTest(filename=filename, kind=kind), tempfile.TemporaryDirectory() as temporary:
                    directory = Path(temporary) / ".ssh"
                    directory.mkdir()
                    victim = Path(temporary) / "unrelated"
                    victim.write_text("do not change")
                    victim.chmod(0o640)
                    target = directory / filename
                    if kind == "fifo":
                        os.mkfifo(target)
                    elif kind == "hardlink":
                        os.link(victim, target)
                    else:
                        target.symlink_to(victim)
                    rc, stderr = self.run_authorization_script(install, directory)
                    self.assertNotEqual(rc, 0)
                    self.assertEqual(victim.read_text(), "do not change")
                    self.assertEqual(stat.S_IMODE(victim.stat().st_mode), 0o640)


if __name__ == "__main__":
    unittest.main(verbosity=2)
