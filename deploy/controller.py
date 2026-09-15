#!/usr/bin/env python3
"""Build, inspect and install a fresh Linux controller; never migrate state.

Build/preflight are read-only except for the explicitly named new bundle.
install-fresh creates a stopped installation. Starting services is a separate,
reviewable administrator action. No command connects to a managed Oracle host.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import grp
import io
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
INSTALL = Path('/opt/oracle-patching')
CONFIG = Path('/etc/oracle-patching')
STATE_PARENT = Path('/var/lib/oracle-patching')
STATE = STATE_PARENT / 'controller'
SSH_HOME = Path('/var/lib/oracle-patching-home')
OLLAMA_HOME = Path('/var/lib/oracle-patching-ollama')
UNIT = Path('/etc/systemd/system/oracle-patching.service')
OLLAMA_UNIT = Path('/etc/systemd/system/opu-ollama.service')
NGINX = Path('/etc/nginx/conf.d/oracle-patching.conf')
USER = 'opu-controller'
SOURCE_DIRS = ('bin', 'lib', 'operations', 'contracts', 'webapp', 'scripts', 'deploy')
EXCLUDE = {'var', '.git', '.venv', '__pycache__', 'node_modules', 'recovery_fixtures'}
MAX_BUNDLE = 64 * 1024 * 1024
INPUT_FILES = ('hosts_file', 'principals_file', 'ssh_config_file', 'known_hosts_file', 'ssh_private_key_file')
REQUIRED_COMMANDS = ('bash', 'jq', 'xmllint', 'tar', 'gzip', 'ssh', 'sha256sum', 'flock', 'systemctl', 'nginx', 'useradd')
# The controller installs jsonschema==4.26.0, whose Requires-Python is >=3.10.
# Managed-host native tools have a separate minimum in lib/opu/python.sh.
MIN_CONTROLLER_PYTHON = (3, 10)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def regular(path: Path, *, admin=False, maximum=MAX_BUNDLE):
    require(path.is_absolute(), 'Input paths must be absolute')
    require(not any(part.is_symlink() for part in (path, *path.parents)), 'Input path cannot contain symbolic links')
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'Input must be a regular file with one link')
    require(info.st_size <= maximum, 'Input exceeds size limit')
    if admin:
        require(info.st_uid == 0 and info.st_mode & 0o022 == 0, 'Installation inputs must be root-owned and not group/world writable')
    return path.read_bytes()


def safe_name(name):
    path = PurePosixPath(name)
    return bool(name and not path.is_absolute() and '..' not in path.parts and str(path) == name and '\\' not in name)


def build(source: Path, output: Path, release_id: str):
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', release_id) is not None and '..' not in release_id, 'Invalid release ID')
    source = source.resolve()
    files, payloads = [], {}
    for directory in SOURCE_DIRS:
        base = source / directory
        require(base.is_dir() and not base.is_symlink(), 'Source directory missing or symbolic: ' + directory)
        for current, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in EXCLUDE)
            require(not any((Path(current) / name).is_symlink() for name in dirs), 'Source directory symlinks are not packaged')
            for name in sorted(names):
                path = Path(current) / name
                if name.startswith(('.', '.env')) or name.endswith(('.pyc', '.log', '.tmp')):
                    continue
                if path.parent == source / 'webapp' and path.suffix == '.json':
                    continue  # Installed inventory and identity config are never shipped.
                require(path.is_file() and not path.is_symlink(), 'Source links or special files are not packaged')
                data = path.read_bytes()
                mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                relative = path.relative_to(source).as_posix()
                files.append({'path': relative, 'sha256': sha(data), 'mode': mode, 'size': len(data)})
                payloads[relative] = data
    files.sort(key=lambda row: row['path'])
    require(sum(row['size'] for row in files) < MAX_BUNDLE, 'Source bundle is too large')
    manifest = {'schema_version': 1, 'release_id': release_id, 'classification': 'controller-deployment-not-certification',
                'source_sha256': sha(canonical(files)), 'files': files}
    payloads['deployment-manifest.json'] = canonical(manifest)
    # Exclusive creation prevents overwriting an earlier reviewed artifact.
    with output.open('xb') as target, tarfile.open(fileobj=target, mode='w:gz') as archive:
        for name, data in sorted(payloads.items()):
            item = tarfile.TarInfo(name)
            item.size, item.mode, item.mtime = len(data), 0o644, 0
            if name != 'deployment-manifest.json':
                item.mode = next(row['mode'] for row in files if row['path'] == name)
            archive.addfile(item, io.BytesIO(data))
    return {'bundle': str(output), 'bundle_sha256': sha(output.read_bytes()), 'source_sha256': manifest['source_sha256'],
            'release_id': release_id, 'file_count': len(files)}


def read_bundle(path: Path, expected_sha: str):
    require(re.fullmatch(r'[a-f0-9]{64}', expected_sha or '') is not None, 'An independently reviewed bundle SHA-256 is required')
    raw = regular(path)
    require(sha(raw) == expected_sha, 'Bundle checksum does not match the reviewed artifact')
    payloads, size = {}, 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        for item in archive:
            require(len(payloads) < 10000 and safe_name(item.name) and item.isfile() and item.name not in payloads,
                    'Archive contains an unsafe, duplicate or excessive member')
            size += item.size
            require(0 <= item.size <= MAX_BUNDLE and size <= MAX_BUNDLE, 'Expanded bundle exceeds size limit')
            payloads[item.name] = archive.extractfile(item).read()
    manifest = json.loads(payloads.pop('deployment-manifest.json'))
    require(manifest.get('schema_version') == 1 and manifest.get('classification') == 'controller-deployment-not-certification', 'Unsupported deployment manifest')
    release_id = manifest.get('release_id', '')
    require(isinstance(release_id, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', release_id) and '..' not in release_id, 'Invalid release ID')
    files = manifest.get('files')
    require(isinstance(files, list) and sha(canonical(files)) == manifest.get('source_sha256'), 'Source manifest checksum mismatch')
    require(len(files) == len(payloads) and {row['path'] for row in files} == set(payloads), 'Manifest member set mismatch')
    for row in files:
        require(row['mode'] in (0o644, 0o755) and row['size'] == len(payloads[row['path']]) and row['sha256'] == sha(payloads[row['path']]), 'Manifest file verification failed')
        require(PurePosixPath(row['path']).parts[0] in SOURCE_DIRS and not any(part in EXCLUDE for part in PurePosixPath(row['path']).parts), 'Package includes excluded state or dependencies')
        require(not (PurePosixPath(row['path']).parent == PurePosixPath('webapp') and row['path'].endswith('.json')), 'Package includes deployment inventory')
    require('webapp/server.py' in payloads and 'deploy/controller.py' in payloads, 'Incomplete controller bundle')
    return manifest, payloads


def load_config(path: Path, *, admin=True):
    config = json.loads(regular(path, admin=admin, maximum=65536))
    require(isinstance(config, dict), 'Deployment configuration must be an object')
    allowed = {*INPUT_FILES, 'public_hostname', 'tls_certificate', 'tls_private_key', 'enable_ollama', 'assistant_config_file'}
    require(set(config) <= allowed, 'Unknown deployment configuration field')
    hostname = config.get('public_hostname', '')
    require(isinstance(hostname, str) and re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?', hostname) and '..' not in hostname, 'Invalid public hostname')
    require(type(config.get('enable_ollama', False)) is bool, 'enable_ollama must be boolean')
    for name in (*INPUT_FILES, 'tls_certificate', 'tls_private_key'):
        value = config.get(name)
        require(isinstance(value, str) and re.fullmatch(r'/[A-Za-z0-9_./-]+', value) and '..' not in Path(value).parts, 'Invalid absolute input path: ' + name)
    for name in (*INPUT_FILES, 'assistant_config_file'):
        if name in config:
            regular(Path(config[name]), admin=admin, maximum=1024 * 1024)
    if config.get('assistant_config_file'):
        assistant = json.loads(Path(config['assistant_config_file']).read_text())
        require(isinstance(assistant, dict) and assistant.get('enabled') is True, 'Supplied assistant configuration must be explicitly enabled')
        require(assistant.get('provider', 'ollama') == 'ollama'
                and assistant.get('base_url', 'http://127.0.0.1:11434/v1') == 'http://127.0.0.1:11434/v1'
                and not assistant.get('allow_private_endpoint') and not assistant.get('allow_insecure_private'),
                'This deployment accepts only the same-server loopback Ollama endpoint')
        model = assistant.get('model')
        require(isinstance(model, str) and model.strip() and not model.startswith('REPLACE_')
                and 'cloud' not in model.lower(), 'Choose an installed local model; cloud and placeholder models are not accepted')
    hosts = json.loads(Path(config['hosts_file']).read_text())
    require(isinstance(hosts, dict) and isinstance(hosts.get('hosts'), list), 'Hosts inventory requires a hosts array')
    principals = json.loads(Path(config['principals_file']).read_text())
    entries = principals.get('principals') if isinstance(principals, dict) else None
    require(isinstance(entries, list) and entries, 'Separate principal credentials are required; no lab token bootstrap')
    actors, digests, roles = set(), set(), {}
    for entry in entries:
        require(isinstance(entry, dict), 'Invalid principal entry')
        actor, digest, assigned = entry.get('actor'), entry.get('token_sha256'), entry.get('roles')
        require(isinstance(actor, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', actor) and actor not in actors, 'Invalid or duplicate principal')
        require(isinstance(digest, str) and re.fullmatch(r'[a-f0-9]{64}', digest) and digest not in digests, 'Distinct hashed credentials are required')
        require(isinstance(assigned, list) and assigned and set(assigned) <= {'viewer', 'requester', 'approver', 'operator', 'admin'}, 'Invalid principal roles')
        actors.add(actor); digests.add(digest)
        expiry = entry.get('expires_at')
        active = not entry.get('disabled', False)
        if expiry:
            expiry = dt.datetime.fromisoformat(expiry.replace('Z', '+00:00'))
            require(expiry.tzinfo is not None, 'Principal expiry requires timezone')
            active = active and expiry > dt.datetime.now(dt.timezone.utc)
        if active:
            roles[actor] = set(assigned)
    require(any(a != b and a != c and b != c for a in roles for b in roles for c in roles
                if 'requester' in roles[a] and 'approver' in roles[b] and 'operator' in roles[c]),
            'Fresh deployment requires distinct active requester, approver and operator identities')
    return config


def fresh_conflicts(enable_ollama=False):
    paths = [INSTALL, CONFIG, STATE_PARENT, SSH_HOME, UNIT, NGINX]
    users = [USER]
    if enable_ollama:
        paths += [OLLAMA_HOME, OLLAMA_UNIT]
        users += ['opu-ollama']
    conflicts = [str(path) for path in paths if path.exists() or path.is_symlink()
                 or any(parent.is_symlink() for parent in path.parents)]
    for user in users:
        try:
            pwd.getpwnam(user)
            conflicts.append('existing service account: ' + user)
        except KeyError:
            pass
        try:
            grp.getgrnam(user)
            conflicts.append('existing service group: ' + user)
        except KeyError:
            pass
    return conflicts


def preflight(manifest, config):
    blockers = []
    if platform.system() != 'Linux' or not Path('/run/systemd/system').is_dir():
        blockers.append('A Linux host booted with systemd is required')
    if sys.version_info < MIN_CONTROLLER_PYTHON:
        blockers.append('Controller dependencies require Python 3.10 or later; use a supported versioned interpreter')
    if importlib.util.find_spec('venv') is None or importlib.util.find_spec('ensurepip') is None:
        blockers.append('Install the OS Python venv/ensurepip package before deployment')
    missing = [name for name in REQUIRED_COMMANDS if shutil.which(name) is None]
    if missing:
        blockers.append('Missing OS prerequisites: ' + ', '.join(missing))
    conflicts = fresh_conflicts(config.get('enable_ollama', False))
    if conflicts:
        blockers.append('Fresh install refuses existing paths/accounts: ' + ', '.join(conflicts))
    for port in (8765, 11434) if config.get('enable_ollama') else (8765,):
        try:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', port))
        except OSError:
            blockers.append('Required loopback port is already in use: ' + str(port))
    try:
        for name in ('tls_certificate', 'tls_private_key'):
            regular(Path(config[name]), admin=True, maximum=1024 * 1024)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(config['tls_certificate'], config['tls_private_key'])
    except (ValueError, OSError, ssl.SSLError):
        blockers.append('TLS certificate/key are missing, unsafe or do not match')
    if config.get('enable_ollama'):
        try:
            binary = Path('/usr/local/bin/ollama')
            info = binary.lstat()
            require(not any(path.is_symlink() for path in (binary, *binary.parents))
                    and stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o022 == 0
                    and os.access(binary, os.X_OK), 'Ollama binary is not protected and executable')
        except (ValueError, OSError):
            blockers.append('Install and review /usr/local/bin/ollama separately before enabling its service')
    return {'status': 'blocked' if blockers else 'ready_for_fresh_install', 'blockers': blockers,
            'release_id': manifest['release_id'], 'source_sha256': manifest['source_sha256'],
            'state_directory': str(STATE), 'existing_state_migration': 'not_supported',
            'services_will_start': False, 'remote_connections': False}


def render_nginx(config, template):
    return template.replace('@@HOSTNAME@@', config['public_hostname']).replace('@@TLS_CERT@@', config['tls_certificate']).replace('@@TLS_KEY@@', config['tls_private_key'])


def install_fresh(manifest, payloads, config):
    require(os.geteuid() == 0, 'install-fresh must run as the Linux administrator')
    result = preflight(manifest, config)
    require(not result['blockers'], '; '.join(result['blockers']))
    # No cleanup trap removes an interrupted install: partial paths deliberately
    # block a rerun until an administrator inspects what was created.
    INSTALL.mkdir(mode=0o755)
    release = INSTALL / 'releases' / manifest['release_id']
    release.mkdir(parents=True, mode=0o755)
    for row in manifest['files']:
        path = release / row['path']
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as target:
            target.write(payloads[row['path']])
        path.chmod(row['mode'])
    (release / 'deployment-manifest.json').write_bytes(canonical(manifest))
    subprocess.run([sys.executable, '-m', 'venv', str(release / '.venv')], check=True)
    subprocess.run([str(release / '.venv/bin/python'), '-m', 'pip', 'install', '--disable-pip-version-check',
                    '-r', str(release / 'scripts/requirements.txt'), '-r', str(release / 'webapp/requirements-sso.txt')], check=True)
    subprocess.run(['useradd', '--system', '--user-group', '--home-dir', str(SSH_HOME), '--create-home', '--shell', '/usr/sbin/nologin', USER], check=True)
    account = pwd.getpwnam(USER)
    STATE.mkdir(parents=True, mode=0o700)
    os.chown(STATE, account.pw_uid, account.pw_gid)
    CONFIG.mkdir(mode=0o750)
    os.chown(CONFIG, 0, account.pw_gid)
    ssh = SSH_HOME / '.ssh'
    ssh.mkdir(mode=0o700)
    os.chown(ssh, account.pw_uid, account.pw_gid)
    copies = {'hosts_file': CONFIG / 'hosts.json', 'principals_file': CONFIG / 'principals.json',
              'ssh_config_file': ssh / 'config', 'known_hosts_file': ssh / 'known_hosts', 'ssh_private_key_file': ssh / 'id_controller'}
    for name, target in copies.items():
        target.write_bytes(regular(Path(config[name]), admin=True, maximum=1024 * 1024))
        target.chmod(0o600 if name.startswith('ssh_') or name == 'known_hosts_file' else 0o640)
        os.chown(target, account.pw_uid if target.parent == ssh else 0, account.pw_gid)
    environment = payloads['deploy/templates/controller.env'].decode()
    if config.get('assistant_config_file'):
        target = CONFIG / 'assistant-config.json'
        target.write_bytes(regular(Path(config['assistant_config_file']), admin=True, maximum=65536))
        target.chmod(0o640); os.chown(target, 0, account.pw_gid)
        environment += 'OPU_ASSISTANT_CONFIG=/etc/oracle-patching/assistant-config.json\n'
    (CONFIG / 'controller.env').write_text(environment)
    (CONFIG / 'controller.env').chmod(0o600)
    (CONFIG / 'deployment.json').write_bytes(canonical(config))
    (CONFIG / 'deployment.json').chmod(0o600)
    UNIT.write_bytes(payloads['deploy/templates/oracle-patching.service'])
    # Keep proxy config staged: no running nginx configuration is overwritten.
    (CONFIG / 'nginx.conf').write_text(render_nginx(config, payloads['deploy/templates/nginx.conf'].decode()))
    if config.get('enable_ollama'):
        subprocess.run(['useradd', '--system', '--user-group', '--home-dir', str(OLLAMA_HOME), '--create-home', '--shell', '/usr/sbin/nologin', 'opu-ollama'], check=True)
        OLLAMA_UNIT.write_bytes(payloads['deploy/templates/opu-ollama.service'])
    (INSTALL / 'current').symlink_to(release)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    return {**result, 'status': 'installed_stopped', 'release_directory': str(release),
            'next': 'Review service preflight, nginx configuration and TLS; explicitly start the services afterward'}


def check_service():
    require(os.geteuid() != 0, 'Controller must run as its unprivileged service account')
    expected = {'OPU_WEBAPP_STATE_DIR': str(STATE), 'OPU_WEBAPP_HOSTS_FILE': str(CONFIG / 'hosts.json'),
                'OPU_WEBAPP_ALLOW_FIXTURES': '0', 'OPU_WEBAPP_RBAC': '1', 'OPU_WEBAPP_PORT': '8765'}
    require(all(os.environ.get(name) == value for name, value in expected.items()), 'Missing deployment state, fixture or authentication settings')
    require(not os.environ.get('OPU_WEBAPP_TOKEN'), 'Shared lab credentials are forbidden in this deployment')
    require(not any(name.endswith('TEST_MODE') and value not in ('', '0') for name, value in os.environ.items() if name.startswith('OPU_')), 'Fixture execution flags are forbidden')
    sys.path.insert(0, str(ROOT / 'webapp'))
    import auth
    import company_auth
    import evidence
    import planctl
    import recoveryctl
    import server
    require(evidence.VAR_DIR == STATE / 'hosts' and planctl.PLAN_STATE_DIR == STATE / 'plans'
            and recoveryctl.LIVE_DIR == STATE / 'recovery-live' and server.HOSTS_FILE == CONFIG / 'hosts.json',
            'Application does not implement the required persistent path hooks')
    require(auth.rbac_enabled(), 'Role-based authentication must be enabled')
    if company_auth.configured():
        company_auth.config()
    else:
        auth.validate_configuration()
    if os.environ.get('OPU_ASSISTANT_CONFIG'):
        import local_llm
        require(local_llm.config_status().get('configured') is True, 'Assistant configuration failed native validation')
    return {'status': 'ready', 'bind': '127.0.0.1:8765', 'state_directory': str(STATE), 'fixtures': 'disabled'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    package = commands.add_parser('build')
    package.add_argument('--source', type=Path, default=ROOT)
    package.add_argument('--output', type=Path, required=True)
    package.add_argument('--release-id', required=True)
    for name in ('preflight', 'install-fresh'):
        action = commands.add_parser(name)
        action.add_argument('--bundle', type=Path, required=True)
        action.add_argument('--sha256', required=True)
        action.add_argument('--config', type=Path, required=True)
    commands.add_parser('check-service')
    args = parser.parse_args()
    try:
        if args.command == 'build':
            result = build(args.source, args.output, args.release_id)
        elif args.command == 'check-service':
            result = check_service()
        else:
            manifest, payloads = read_bundle(args.bundle.absolute(), args.sha256)
            config = load_config(args.config.absolute())
            result = preflight(manifest, config) if args.command == 'preflight' else install_fresh(manifest, payloads, config)
        print(json.dumps(result, indent=2))
        return 1 if result.get('status') == 'blocked' else 0
    except (ValueError, OSError, KeyError, TypeError, tarfile.TarError, subprocess.CalledProcessError) as error:
        # Credentials/configuration content and external command output are not printed.
        print(json.dumps({'status': 'blocked', 'error': str(error)}), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
