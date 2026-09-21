"""Typed runtime receipts for isolated controller transport fixtures.

These helpers never install tools or contact a host. Installer behavior has
separate subprocess/concurrency tests in runtime_package.py.
"""

DIGEST = "a" * 64


def runtime_receipt(ssh_alias, remote_root, sudo=False, **kwargs):
    return {"ssh_alias": ssh_alias, "fingerprint": DIGEST,
            "runtime_root": remote_root.rstrip("/") + "/.opu-runtimes/" + DIGEST,
            "synced": False, "cached": False}


def host_runtime_receipts(host, **kwargs):
    aliases = [host["ssh_alias"], *(node["ssh_alias"] for node in host.get("nodes", []))]
    return [runtime_receipt(alias, host["remote_root"], host.get("sudo", False))
            for alias in dict.fromkeys(aliases)]
