#!/usr/bin/env python3
"""Legacy remote analyze helpers preserve literal arguments at SSH's shell boundary."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def function(script, name):
    start = script.index(name + "() {")
    line_end = script.index("\n", start)
    if script[start:line_end].endswith(" }"):
        return script[start:line_end]
    return script[start:script.index("\n}", start) + 2]


# Emulate only SSH's documented shell-command boundary locally. There is no
# network command, Oracle binary, privileged program, or machine credential.
DOUBLES = r'''
local_node() { return 1; }
is_local() { return 1; }
sudo() { if [ "$1" = -u ]; then shift 2; fi; if [ "$1" = -n ]; then shift; fi; "$@"; }
runuser() { shift 2; [ "$1" != -- ] || shift; "$@"; }
ssh() {
    while [ "$1" = -o ]; do shift 2; done
    shift
    [ "${1:-}" != -- ] || shift
    /bin/bash -c "$*"
}
export -f sudo runuser
'''

with tempfile.TemporaryDirectory(prefix="opu-remote-args-") as temporary:
    marker = Path(temporary) / "unexpected-shell-evaluation"
    arguments = ["/home/Oracle install", "", "a'b", '"quoted"',
                 f"value; : > {marker}; #", f"$(touch {marker})", "line\nbreak", "*"]
    for tool, helpers in (("opu-database-rolling-patch", ("root", "dbuser")),
                          ("opu-grid-rolling-patch", ("run_root", "run_grid"))):
        script = (ROOT / "bin" / tool).read_text()
        source = ". " + shlex.quote(str(ROOT / "lib/opu/common.sh")) + "\n" + DOUBLES
        source += "\n".join(function(script, name) for name in helpers) + "\n"
        source += 'TEST_MODE=1; DB_OWNER="fixture owner"; GRID_OWNER="fixture owner"; REMOTE_USER="$1"; shift; '
        for helper in helpers:
            for remote_user in ("root", "fixture-operator"):
                command = source + helper + ' remote-node.invalid /usr/bin/printf "%s\\0" "$@"'
                result = subprocess.run(["bash", "-c", command, "fixture", remote_user, *arguments],
                                        capture_output=True, timeout=10, env=os.environ)
                assert not marker.exists(), (tool, helper, remote_user, "remote shell evaluated an argument")
                assert result.returncode == 0, (tool, helper, remote_user, result.stderr)
                assert result.stdout == b"".join(value.encode() + b"\0" for value in arguments), (tool, helper, remote_user, result.stdout)

print("rolling remote arguments: 8 paths passed; arguments remain literal")
