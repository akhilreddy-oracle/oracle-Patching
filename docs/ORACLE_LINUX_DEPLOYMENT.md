# Oracle Linux 8.10 deployment supplement

Use this with [Remote controller deployment](REMOTE_DEPLOYMENT.md). It prepares
a fresh controller on Oracle Linux 8.10 x86_64; it does not migrate the existing
controller or resume its plans. These are administrator-run instructions, not a
record of a completed deployment. RAM, GPU, available disk, normal SSH access,
TLS arrangement and managed-host service access must be established on the
actual server. A working OCI serial console alone does not establish `opc` SSH
access.

## 1. Collect server capabilities

**Workstation, in the reviewed repository**, after recovering normal SSH:

```bash
ssh -T opc@PATCHING_SERVER 'bash -s' < deploy/server-preflight.sh
```

Replace `PATCHING_SERVER` with the configured host or alias and verify its SSH
host key through the administrator's trusted channel. Inspect the resulting
OS, CPU, RAM, disk, GPU, Python, package and installation-conflict observations.
This does not install packages or contact managed Oracle databases.

## 2. Install the Oracle Linux prerequisites

**Patching server, normal `opc` shell with sudo**, inspect repositories and
available packages before choosing a transaction:

```bash
dnf repolist
dnf list --available python3.12 python3.12-pip
dnf module list nginx
rpm -q python3.12 python3.12-pip nginx
```

Use **Python 3.12** for this Oracle Linux 8.10 deployment. Oracle documents the
packages `python3.12` and `python3.12-pip`, and the `nginx:1.24` module stream.
Install the missing prerequisites after reviewing the transaction:

```bash
sudo dnf install python3.12 python3.12-pip bash jq libxml2 tar gzip \
  openssh-clients coreutils util-linux systemd shadow-utils curl \
  policycoreutils policycoreutils-python-utils libselinux-utils
```

For a fresh server without an existing nginx installation or conflicting
enabled stream:

```bash
sudo dnf module install nginx:1.24
```

If packages are unavailable, have the repository administrator check the
approved Oracle Linux 8 BaseOS/AppStream repositories or internal mirrors. If
another nginx stream or web service is installed, review that deployment before
changing streams; this supplement does not reset modules or replace existing
web services. [Oracle Linux 8.10 package instructions](https://docs.oracle.com/en/operating-systems/oracle-linux/8/relnotes8.10/ol8-features-DynamicProgramming.html)

The default `python3` on Oracle Linux 8 can still be Python 3.6. Always invoke
`python3.12` explicitly below; retain the system interpreter and its aliases.
Oracle also documents `python3.11`/`python3.11-pip` and the older
`python39`/`python38` module names, but these are not the fresh-install choice
here. The pinned `jsonschema==4.26.0` dependency requires Python 3.10 or later;
Python 3.9 cannot install the current dependency set and is rejected by
controller preflight. Oracle lists the 3.11, 3.9 and 3.8 application streams as
retired; 3.12 is listed as a full-life stream. Package availability is not a
support guarantee for an older runtime.
[Oracle Python installation](https://docs.oracle.com/en/operating-systems/oracle-linux/8/python/python-InstallingPython.html),
[older module names](https://docs.oracle.com/en/operating-systems/oracle-linux/8/relnotes8.4/ol8-NewFeaturesandChanges.html),
[application stream lifecycle](https://docs.oracle.com/en/operating-systems/oracle-linux/product-lifecycle/ol8_application_streams.html)

Verify the interpreter and the modules used by the installer:

```bash
python3.12 --version
python3.12 -c 'import ensurepip, ssl, venv; print("venv, ensurepip and TLS modules available")'
python3.12 -m pip --version
```

The installer creates an isolated venv with that interpreter and installs
`scripts/requirements.txt` plus `webapp/requirements-sso.txt` there. Keep pip
changes inside that venv. Its root-run pip process must have access to the
approved package index or configured internal wheel repository; an `opc`-only
pip configuration is insufficient. [Oracle virtual-environment guidance](https://docs.oracle.com/en/operating-systems/oracle-linux/8/python/python-InstallingThirdPartyPackages.html)

## 3. Prepare inputs and admit the bundle

Follow the input-file and artifact-integrity steps in
[Remote controller deployment](REMOTE_DEPLOYMENT.md). Transfer the reviewed
bundle and matching deployment tooling through the normal administrator channel.
Required inputs include a public HTTPS hostname and matching certificate/key,
inventory, separate requester/approver/operator credential digests, and a
dedicated controller SSH identity with verified managed-host keys. Root owns
the input files; paths must be absolute and contain no symlinks. Generate the
service key on the server or use an approved service identity. Keep the Mac's
administrator private key on the Mac.

**Patching server, in the transferred reviewed checkout/tooling directory:**

```bash
sudo /usr/bin/python3.12 -B deploy/controller.py preflight \
  --bundle /root/oracle-patching-controller.tar.gz \
  --sha256 REPLACE_WITH_INDEPENDENTLY_REVIEWED_ARCHIVE_SHA256 \
  --config /root/opu-install-inputs/deployment.json
```

Proceed only when this reports `ready_for_fresh_install` without blockers. The
SHA-256 must come from the separately reviewed build output. An existing
installation, account or partial install requires investigation rather than
deletion or an automatic retry. Once the result and inputs are reviewed, use
the same command with `install-fresh` in place of `preflight`. Its expected
terminal result is `installed_stopped`; it does not start the controller,
nginx, Ollama, or any Oracle operation.

## 4. Review TLS, SELinux and network access

**Patching server:** review the staged nginx configuration at
`/etc/oracle-patching/nginx.conf`. The controller binds to `127.0.0.1:8765`;
nginx serves HTTPS on 443. The template's port-80 server rejects HTTP requests;
it does not require public port-80 access. Check existing nginx default servers
before activation. Preserve existing configurations for administrator review.

Inspect the SELinux state and nginx network permission:

```bash
getenforce
getsebool httpd_can_network_connect
```

On an enforcing server, nginx must be permitted to connect to the loopback
upstream. Oracle's nginx example uses this persistent boolean:

```bash
sudo setsebool -P httpd_can_network_connect on
```

That boolean permits outbound connections for the HTTP service domain; it is
broader than this application's single upstream. Apply it only as the site's
chosen policy, or have its SELinux administrator provide a narrower policy.
Keep SELinux enforcing. Inspect denials and file labels when resolving a 502
or certificate-read failure; do not generate blanket allow rules or relabel
the controller's private state as web content.
[Oracle nginx SELinux example](https://docs.oracle.com/en/learn/oci-opensearch-dashboards/index.html),
[SELinux administration utilities](https://docs.oracle.com/en/operating-systems/oracle-linux/selinux/selinux-SELinuxUtilities.html)

TLS files in the configured `/etc/pki/tls` paths must have the expected labels
and root-protected permissions. Review their labels with `ls -lZ` and compare
against `matchpathcon`; use `restorecon` on the specific certificate/key paths
when the approved policy requires it. Verify certificate SAN, expiry and client
trust as well as the key match checked by preflight.

Review the active firewalld zone and OCI network rules through the site's normal
network-change process. Permit HTTPS only from the intended client networks,
retain administrator SSH access, and allow the required outbound managed-host,
package-index and identity-provider connections. Do not open controller port
8765 or model port 11434 to remote clients. This guide does not change cloud
rules or disable the host firewall.

## 5. Activate and verify the fresh controller

**Patching server:** after reviewing the stopped installation, publish the
staged nginx config as `/etc/nginx/conf.d/oracle-patching.conf` only if that
destination is absent. Check the resulting configuration and unit:

```bash
sudo nginx -t
sudo systemd-analyze verify /etc/systemd/system/oracle-patching.service
```

Only after those checks pass, start `oracle-patching.service` and start or
reload nginx according to the site's service-change procedure. The controller
unit runs its native configuration check before starting the application.
Inspect status and listening addresses without printing configuration secrets:

```bash
sudo systemctl status oracle-patching.service nginx --no-pager
sudo ss -ltnp
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8765/api/hosts
```

The unauthenticated API check should return **401**. From an intended client,
open the configured HTTPS hostname with normal certificate verification and
test each role independently. Confirm fixtures are disabled, the inventory
contains the intended hosts, and the new controller has no old plan queued for
execution. Collect fresh discovery through the application before creating a
new maintenance plan.

## 6. Add the local model and prove the live workflow

Size and select a local model only after the hardware preflight is reviewed.
The installer downloads neither Ollama nor model weights. Follow
[Local LLM configuration](LOCAL_LLM.md) and the optional-model section of
[Remote controller deployment](REMOTE_DEPLOYMENT.md); keep inference on
loopback and verify cloud access is disabled. A working simulator test does not
establish actual-model tool accuracy, acceptable latency or available capacity.
Use the [model protocol assessment](MODEL_ACCEPTANCE.md) for the initial
synthetic response checks against the installed model. It does not execute
patching actions or replace the live acceptance cycle below.

Live acceptance still needs a reviewed backup-to-patch cycle through the
application, native task results, database/listener verification and retained
before-and-after reports. RMAN backup checks and `RESTORE DATABASE VALIDATE`
verify backup readability/selection; they do **not** restore and open a separate
database. Record an actual restore drill separately if required. Keep fixture
tests, live-lab verification and production approval distinct, as specified in
[Release validation](RELEASE_VALIDATION.md) and
[Recovery preparation](RECOVERY_PREPARATION.md).
