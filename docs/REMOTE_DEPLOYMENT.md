# Remote controller deployment

The deployment tooling prepares a **fresh, stopped Linux controller**. It does
not install over an existing controller, move sealed evidence, start Oracle
work, or grant production certification. Controller and local model run on the
same server. Replace `PATCHING_SERVER` and the administrator username below with
your configured deployment target.

The preflight reads machine capabilities and installation conflicts without
printing keys, environment secrets, database data or configuration contents:

```bash
ssh -T opc@PATCHING_SERVER 'bash -s' < deploy/server-preflight.sh
```

Do not disable SSH host-key checking. Verify the server's fingerprint through a
trusted channel before accepting a first connection. Provide the preflight
output, public HTTPS hostname/TLS certificate arrangement, and whether this is
a fresh controller or a deliberate cutover from an existing one. Adding an
administrator public key authorizes access to the server; it does not provision
the controller's separate identity for managed databases.

## Layout and application interface

| Path | Owner and purpose |
| --- | --- |
| `/opt/oracle-patching/releases/<release-id>` | Root-owned code and Python virtual environment; each release has a source manifest |
| `/opt/oracle-patching/current` | Administrator-selected release symlink |
| `/var/lib/oracle-patching/controller` | `opu-controller`, persistent controller state and sealed evidence |
| `/etc/oracle-patching` | Root-owned inventory, credential digests and configuration |
| `/var/lib/oracle-patching-home/.ssh` | Dedicated service SSH config, key and verified known hosts |
| `/run/oracle-patching` | Service-owned temporary files |
| `/var/lib/oracle-patching-ollama` | Optional separate `opu-ollama` identity and model storage |

The service sets `OPU_WEBAPP_STATE_DIR`, `OPU_WEBAPP_HOSTS_FILE`,
`OPU_WEBAPP_ALLOW_FIXTURES=0`, `OPU_WEBAPP_RBAC=1` and a principal registry path.
Its `ExecStartPre` verifies the effective application paths and authentication
mode. The existing application binds to `127.0.0.1:8765`; nginx terminates TLS
on 443. The proxy does not expose Ollama. Fixtures cannot be created/executed
through the deployed application. There is no shared lab-token bootstrap.

The unit is unprivileged with a read-only system view and explicit writable
state/home/runtime directories. It permits IPv4/IPv6/Unix sockets, SSH child
processes, DNS, HTTPS company login and loopback inference. Local privilege
escalation is denied; managed-host privileged commands still require the
separately configured remote automation account and existing native gates.
See the [systemd execution settings](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).

## Build and review an artifact

From the reviewed checkout:

```bash
python3 -B deploy/controller.py build \
  --release-id REPLACE_WITH_REVIEWED_COMMIT \
  --output /tmp/oracle-patching-controller.tar.gz
```

The output reports the archive SHA-256 and source-manifest SHA-256. Keep the
reviewed archive digest independently when transferring the bundle. Packaging
excludes local state, installation inventory, credentials, caches, dependencies
and executable Oracle fixture assets. Existing output archives are never
overwritten. The manifest records exact file bytes and modes; it is an integrity
record, not a signed approval or production certificate.

## Prepare server inputs

Install OS packages before running the installer: Python 3.9+ with venv and
ensurepip, Bash, jq, libxml2 (`xmllint`), tar/gzip, OpenSSH client, coreutils
(`sha256sum`), util-linux (`flock`), systemd and nginx. Package names differ
between Oracle Linux and Ubuntu; use the preflight output to choose the matching
repositories. Python dependencies come from `scripts/requirements.txt` and
`webapp/requirements-sso.txt` in the verified bundle. The installer uses pip in a
new release-specific virtual environment; approved package-index connectivity
or a configured internal wheel repository is required.

Prepare root-owned input files that are not group/world writable, then adapt
`deploy/templates/deployment.json.example`. Input paths must be absolute and
contain no symlinks. Store secrets with mode 0600. The installer copies the
reviewed inventory and separate principal credential digests into protected
configuration files. It requires distinct active requester, approver and
operator identities so the backup/patch approval chain can function. Provision
random credentials privately; do not put raw tokens in JSON, shell history,
the package, screenshots or chat. Existing [company-login configuration](COMPANY_LOGIN.md)
can be added later without choosing an identity provider now.

Use a **dedicated controller-to-managed-host key generated on the server** or
an already approved service identity. Never copy the workstation/admin
`id_rsa` private key to this host. Authorize only the service public key on the
intended managed hosts. Prepare the service SSH config from
`deploy/templates/ssh_config.example`, and obtain managed-host fingerprints
through a trusted channel. Review aliases, destination users, remote tool
roots, and any jump-host configuration before installation. No install command
connects to managed hosts or modifies their `authorized_keys`/sudo policy.

The TLS certificate and matching private key stay at the explicit configured
paths. Preflight checks that they load as a pair; the administrator must also
verify the public hostname/SAN, expiry and client trust chain. On enforcing
SELinux systems, review nginx's permission to reach localhost:8765 and apply
the appropriate site policy; do not disable SELinux. Review firewall ingress
for HTTPS, administrator SSH access, and required outbound managed-host/IdP
traffic. Ports 8765 and 11434 remain loopback-only.

## Read-only admission and fresh install

Transfer the reviewed bundle and tooling using your normal administrator
channel. On the server, run these commands from the reviewed source/tooling:

```bash
sudo python3 -B deploy/controller.py preflight \
  --bundle /root/oracle-patching-controller.tar.gz \
  --sha256 REPLACE_WITH_REVIEWED_ARCHIVE_SHA256 \
  --config /root/opu-install-inputs/deployment.json
```

Preflight verifies archive integrity, rejects unsafe/duplicate archive members,
validates credentials, checks prerequisites, TLS and occupied loopback ports,
and refuses existing installation/state paths or service accounts. It creates
no files and does not contact Oracle hosts. Inspect its result before the
separate installation step:

```bash
sudo python3 -B deploy/controller.py install-fresh \
  --bundle /root/oracle-patching-controller.tar.gz \
  --sha256 REPLACE_WITH_REVIEWED_ARCHIVE_SHA256 \
  --config /root/opu-install-inputs/deployment.json
```

The installer repeats admission, creates the dedicated accounts/directories,
installs the versioned code/venv, and writes a **stopped** service plus a staged
nginx config. It only reloads systemd's unit definitions. An interrupted install
leaves its paths for inspection; rerunning never deletes or overwrites them.

After reviewing the generated files, install the staged nginx configuration
only if `/etc/nginx/conf.d/oracle-patching.conf` is still absent. Validate it with
`nginx -t` and validate the unit with `systemd-analyze verify`. Then explicitly
start `oracle-patching.service` and reload/start nginx through the site's usual
change procedure. Confirm unauthenticated API access is rejected and that each
principal has the expected roles through the HTTPS application. First collect
fresh read-only discovery through the app. No old run should resume merely
because a new controller has started.

## Optional local model

Choose a model after reviewing server RAM/GPU/disk capacity. Install a reviewed
Ollama binary at `/usr/local/bin/ollama` separately; this installer downloads no
binary or model. Set `enable_ollama: true` only when that binary is available.
Supply `assistant_config_file` pointing at a completed copy of
`deploy/templates/assistant-config.json.example` after the selected local model
is installed. The endpoint is strictly `http://127.0.0.1:11434/v1`. Cloud models,
external inference endpoints and placeholder model names are refused.

The optional service sets `OLLAMA_HOST=127.0.0.1:11434` and
`OLLAMA_NO_CLOUD=1`; both settings are documented in the
[Ollama FAQ](https://docs.ollama.com/faq). Its baseline permits CPU inference.
GPU access needs a hardware-specific service override and verification. Install
model files under the dedicated model store through the operator's approved
download process, then start the service and verify its local inventory and
that its logs report cloud disabled. Do not publish port 11434, add a public
proxy route to it, or enable cloud fallback. The application's typed workflow
actions and separate actor approvals remain authoritative.

## Existing controllers and version changes

Sealed plans bind **absolute paths and hashes** for readiness, policy, artifacts,
recovery, task results and authority records. A `webapp/var` symlink into a new
release directory does not make those path bindings safely portable.

The fresh installer therefore has no migration, upgrade, resume, rollback or
force-overwrite option. Existing controller state requires an explicit cutover
inventory: every plan/run state, unresolved execution, remote custody record,
original canonical path, principal/authorizer identity and current window.
Keep the old controller and its history intact. Do not rewrite sealed paths,
re-sign old files, copy paused runs to a new prefix, extend old windows, or
blindly change `current` while work is running. Native input hashes and remote
runtime custody must still match the originally approved plan.

A later code update can be staged in a new release directory while state stays
at its established canonical path. Activation or switching back needs a
reviewed compatibility/quiescence check and an explicit service change. The
current tooling deliberately implements only fresh installation, because an
automatic version switch could invalidate in-flight native/runtime custody.
Fixture release validation and deployment integrity do not establish live-lab
acceptance or production approval.
