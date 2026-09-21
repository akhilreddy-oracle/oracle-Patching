#!/bin/bash
# Start an already configured Mac installation; never install or run patch work.
set -eu
TASK_ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)"
exec "$TASK_ROOT/.venv/bin/python" -B - "$TASK_ROOT" "$@" <<'PY'
import argparse
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import sysconfig
import time
import urllib.request

ROOT = Path(sys.argv[1])
parser = argparse.ArgumentParser(prog="start-local-mac.command", description="Start the configured Mac patching app and its dedicated local model service.")
parser.add_argument("--check-only", action="store_true", help="Check both services without starting them or opening a browser")
parser.add_argument("--copy-token", action="store_true", help="Copy the requester credential to the Mac clipboard for the Session field")
args = parser.parse_args(sys.argv[2:])
if args.check_only and args.copy_token:
    parser.error("--check-only cannot copy a credential")
if sys.platform != "darwin":
    raise SystemExit("This launcher is for macOS.")

VAR = ROOT / "webapp/var"
LOCAL = VAR / "mac-local"
PYTHON = ROOT / ".venv/bin/python"
OLLAMA = Path("/Applications/Ollama.app/Contents/Resources/ollama")
TOKEN = LOCAL / "credentials/akhil-local.token"
URL = "http://127.0.0.1:8765/#/assistant"
os.umask(0o077)


def fail(message):
    raise SystemExit(message)


def check_api_dependencies():
    # Check the reviewed pins before starting either service. Importing also
    # catches incompatible/missing native dependencies such as pydantic-core.
    requirements = ROOT / "webapp/requirements-api.txt"
    try:
        for line in requirements.read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            name, separator, version = line.partition("==")
            if not separator or not name or not version:
                raise ValueError("Invalid API dependency pin")
            if importlib.metadata.version(name) != version:
                raise ValueError("API dependency version differs from the reviewed pin")
            importlib.import_module(name)
    except (ImportError, OSError, ValueError):
        fail("Controller API dependencies are missing or inconsistent. From the repository, run "
             ".venv/bin/python -m pip install -r scripts/requirements.txt -r webapp/requirements-sso.txt "
             "-r webapp/requirements-api.txt, then .venv/bin/python -m pip check. "
             "This launcher does not install software.")


def protected_file(path, *, private=False):
    try:
        value = path.lstat()
    except OSError:
        fail(f"Required local setup file is missing: {path}")
    forbidden = 0o077 if private else 0o022
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_uid != os.getuid() or value.st_mode & forbidden:
        fail(f"Local setup file has unsafe ownership or permissions: {path}")


def private_output(path, flags):
    descriptor = os.open(path, flags | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    value = os.fstat(descriptor)
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_uid != os.getuid() or value.st_mode & 0o077:
        os.close(descriptor)
        fail(f"Refusing an unsafe runtime file: {path}")
    return descriptor


def command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        fail("Unable to inspect local processes. Run this launcher directly in Terminal.")
    return result.stdout.strip()


def expected_commands(executable, suffix):
    candidates = {executable, executable.resolve()}
    # macOS framework builds exec their app-bundle binary; ps then reports that
    # path instead of the venv launcher. Derive only this interpreter's exact
    # framework path from its build metadata, never accept an arbitrary Python.
    if executable.resolve() == Path(sys.executable).resolve():
        framework = sysconfig.get_config_var("PYTHONFRAMEWORK")
        install = sysconfig.get_config_var("PYTHONFRAMEWORKINSTALLDIR")
        version = sysconfig.get_config_var("VERSION")
        bindir = sysconfig.get_config_var("BINDIR")
        if all(isinstance(value, str) and value for value in (framework, install, version, bindir)):
            version_root = Path(install) / "Versions" / version
            if (Path(bindir).resolve() == executable.resolve().parent
                    and version_root.resolve() == Path(bindir).resolve().parent):
                binary = version_root / "Resources" / (framework + ".app") / "Contents/MacOS" / framework
                if binary.is_file():
                    candidates.update({binary, binary.resolve()})
    return {str(candidate) + suffix for candidate in candidates}


def verified_listener(port, pid_file, executable, suffix):
    raw = command(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"])
    listeners = set(raw.splitlines())
    if not listeners:
        # A failed process inventory must not be mistaken for a vacant port.
        with socket.socket() as probe:
            # Match HTTPServer's reuse setting so recently closed connections
            # in TIME_WAIT do not make a stopped controller look occupied.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                fail(f"Port {port} is occupied but its owner could not be verified. Nothing was stopped.")
        if pid_file.exists() or pid_file.is_symlink():
            protected_file(pid_file, private=True)
            previous = pid_file.read_text().strip()
            if not previous.isdigit():
                fail(f"Invalid PID file: {pid_file}")
            if command(["/bin/ps", "-p", previous, "-o", "pid="]):
                fail(f"The recorded process for port {port} is alive but not listening. Inspect it before trying again.")
        return False
    protected_file(pid_file, private=True)
    expected = pid_file.read_text().strip()
    if not expected.isdigit() or listeners != {expected}:
        fail(f"Port {port} is owned by an unverified process. Nothing was stopped or replaced.")
    owner = command(["/bin/ps", "-p", expected, "-o", "uid="])
    actual = command(["/bin/ps", "-ww", "-p", expected, "-o", "command="])
    accepted = expected_commands(executable, suffix)
    if owner != str(os.getuid()) or actual not in accepted:
        fail(f"The process on port {port} does not match this installation. Nothing was stopped.")
    names = command(["/usr/sbin/lsof", "-nP", "-a", "-p", expected, f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"])
    addresses = {line[1:] for line in names.splitlines() if line.startswith("n")}
    if addresses != {f"127.0.0.1:{port}"}:
        fail(f"Port {port} is not bound exclusively to the expected loopback address.")
    return True


def get_json(url, credential=None):
    headers = {"Authorization": "Bearer " + credential} if credential else {}
    request = urllib.request.Request(url, headers=headers)
    # Local checks do not inherit HTTP proxy settings or follow redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *unused, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=5) as response:
        return json.load(response)


def start_service(executable, arguments, port, pid_file, log_file, environment, cwd, health_url):
    if args.check_only:
        fail(f"Service on port {port} is stopped. Run this launcher without --check-only to start it.")
    descriptor = private_output(log_file, os.O_WRONLY | os.O_APPEND)
    with os.fdopen(descriptor, "ab") as log:
        process = subprocess.Popen([str(executable), *arguments], cwd=cwd, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True)
    descriptor = private_output(pid_file, os.O_WRONLY)
    with os.fdopen(descriptor, "w") as stream:
        stream.truncate(0)
        stream.write(str(process.pid) + "\n")
    for _ in range(60):
        if process.poll() is not None:
            fail(f"Service on port {port} exited. Inspect {log_file}")
        try:
            get_json(health_url)
            break
        except (OSError, ValueError):
            time.sleep(0.5)
    else:
        fail(f"Service on port {port} has not become ready. Inspect {log_file}; it was not killed.")


check_api_dependencies()

for directory in (VAR, LOCAL, LOCAL / "models", LOCAL / "credentials"):
    if not directory.is_dir() or directory.is_symlink():
        fail(f"Complete the local setup first; missing or unsafe directory: {directory}")
    metadata = directory.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        fail(f"Local setup directory has unsafe ownership or permissions: {directory}")
for path in (VAR / "principals.json", VAR / "assistant-config.json", TOKEN):
    protected_file(path, private=True)
if not OLLAMA.is_file() or not os.access(OLLAMA, os.X_OK):
    fail("The configured Ollama application binary is missing. This launcher does not install software.")

# Explicit local configuration avoids inheriting another controller's settings.
environment = {key: value for key, value in os.environ.items() if not key.startswith("OPU_")}
environment.pop("TEST_MODE", None)
environment.update({"OPU_WEBAPP_STATE_DIR": str(VAR), "OPU_WEBAPP_HOSTS_FILE": str(ROOT / "webapp/hosts.json"),
                    "OPU_WEBAPP_PRINCIPALS_FILE": str(VAR / "principals.json"),
                    "OPU_ASSISTANT_CONFIG": str(VAR / "assistant-config.json"),
                    "OPU_WEBAPP_RBAC": "1", "OPU_WEBAPP_PORT": "8765", "OPU_PRODUCTION_MODE": "0",
                    "OPU_TEST_MODE": "0"})
os.environ.update({key: value for key, value in environment.items() if key.startswith("OPU_")})
for key in list(os.environ):
    if key.startswith("OPU_") and key not in environment:
        del os.environ[key]
sys.path.insert(0, str(ROOT / "webapp"))
import auth
import local_llm
auth.validate_configuration()
config = local_llm.load_config()
if not config["enabled"] or config["provider"] != "ollama" or config.get("base_url") != "http://127.0.0.1:11435/v1":
    fail("Assistant configuration does not select the dedicated local Ollama service on port 11435.")
if config.get("api_key_env"):
    fail("This Mac launcher expects the dedicated local model endpoint without an API credential.")
credential = TOKEN.read_text().strip()
if auth.require_api_auth("Bearer " + credential) != "akhil-local":
    fail("The requester credential does not match its configured identity.")

ollama_live = verified_listener(11435, LOCAL / "ollama.pid", OLLAMA, " serve")
app_live = verified_listener(8765, VAR / "server.pid", PYTHON, " -B -u " + str(ROOT / "webapp/server.py"))
if not ollama_live:
    model_environment = {key: value for key, value in os.environ.items() if not key.startswith(("OLLAMA_", "OPU_"))}
    model_environment.update({"OLLAMA_HOST": "127.0.0.1:11435", "OLLAMA_MODELS": str(LOCAL / "models"),
                              "OLLAMA_NO_CLOUD": "1", "OLLAMA_CONTEXT_LENGTH": "16384",
                              "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_KEEP_ALIVE": "5m"})
    start_service(OLLAMA, ["serve"], 11435, LOCAL / "ollama.pid", LOCAL / "ollama.log",
                  model_environment, "/", "http://127.0.0.1:11435/api/version")
    verified_listener(11435, LOCAL / "ollama.pid", OLLAMA, " serve")
models = get_json("http://127.0.0.1:11435/api/tags").get("models", [])
names = {item.get("name") for item in models if isinstance(item, dict)}
if config["model"] not in names:
    fail("The configured model is not installed in the dedicated service. This launcher never downloads models.")
if not app_live:
    start_service(PYTHON, ["-B", "-u", str(ROOT / "webapp/server.py")], 8765,
                  VAR / "server.pid", VAR / "server.log", environment, str(ROOT), "http://127.0.0.1:8765/api/health")
    verified_listener(8765, VAR / "server.pid", PYTHON, " -B -u " + str(ROOT / "webapp/server.py"))
session = get_json("http://127.0.0.1:8765/api/session", credential)
assistant = get_json("http://127.0.0.1:8765/api/assistant/config", credential)
if session.get("mode") != "principal" or session.get("actor") != "akhil-local" or not session.get("rbac_enabled"):
    fail("The running controller does not authenticate the expected individual requester.")
if not all(assistant.get(key) is True for key in ("enabled", "configured", "can_chat")) or assistant.get("model") != config["model"]:
    fail("The running controller does not have the expected assistant configuration.")
print("Local application and configured model are available.")
print(URL)
print("This readiness check does not run model inference or any Oracle operation.")
if args.copy_token:
    subprocess.run(["/usr/bin/pbcopy"], input=credential.encode(), check=True)
    print("Requester credential copied. Paste it into Session > API token.")
if not args.check_only:
    subprocess.run(["/usr/bin/open", URL], check=True)
    if not args.copy_token:
        print("Use --copy-token to copy the requester credential without displaying it.")
PY
