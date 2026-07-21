#!/usr/bin/python3
import fcntl
import os
import sys

if sys.argv[1:] != ["-n", "8"]:
    raise SystemExit(64)

try:
    fcntl.flock(8, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)

with open(os.environ["FLOCK_PROBE"], "a", encoding="utf-8") as marker:
    marker.write("acquired\n")
