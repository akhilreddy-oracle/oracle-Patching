# Running the local Mac installation

This workspace has a Mac launcher for the configured application and its
dedicated Ollama service. Runtime configuration, credentials, models and logs
remain under ignored `webapp/var/`; they are not part of the Git repository.
Cloning the repository alone does not reproduce this private local setup.

From the repository root, run:

```bash
./scripts/start-local-mac.command --copy-token
```

The launcher opens <http://127.0.0.1:8765/#/assistant> and, with `--copy-token`,
copies the requester credential to the Mac clipboard. Paste it into **Session →
API token**. The session should show **akhil-local**; its roles are requester
and viewer. The token is never printed or placed in a URL. Omit `--copy-token`
when the browser already has the correct credential.

Double-clicking `scripts/start-local-mac.command` in Finder also launches the
application. After a Mac restart, run the launcher again. It does not install a
login service or LaunchAgent.

## What the launcher checks

It starts a service only when its port is free. Before reusing a listener, it
checks the saved PID, Unix owner, executable or controller command, and loopback
binding. An occupied port with an unverified owner stops the launcher; it never
kills or replaces that process. It then checks the configured model is installed
and the application accepts the individual requester credential.

For a check that starts nothing and opens no browser:

```bash
./scripts/start-local-mac.command --check-only
```

This checks availability and configuration, not model quality or patching
success. It does not send a model prompt, refresh SSH evidence, approve a plan,
resume work or patch a database. For the separate model protocol assessment,
follow [MODEL_ACCEPTANCE.md](MODEL_ACCEPTANCE.md).

## Mac verification on 2026-09-17

The M1 Pro Mac with 16 GB memory runs `qwen3:4b-instruct` through Ollama 0.34.0.
The model is stored locally and the dedicated service disables cloud features.
Real application checks verified listing the three configured hosts, explaining
an existing paused plan, and preparing an exact discovery action card without
execution. The diagnostic action cards were dismissed, and all 249 saved plan
files remained byte-identical. Related automated checks passed (58 tests), as
did lint and the launcher's read-only readiness check.

These checks found and fixed a prompt compatibility issue: the assistant now
places its policy and action context in one initial system message. With the
previous trailing system message, this model incorrectly reported an empty
estate. The separate strict single-response protocol assessment still passed
only **1 of 6** cases; other responses made additional read-only lookups or
returned refusals outside the required exact format. Its failed receipts are
retained under `webapp/var/mac-local/`. Do not treat the model as fully validated
for autonomous patching. Native authorization and confirmation remain required.

## Local services and files

| Component | Location |
| --- | --- |
| Application | `http://127.0.0.1:8765` |
| Dedicated local inference | `http://127.0.0.1:11435/v1` |
| Assistant configuration | `webapp/var/assistant-config.json` |
| Individual identity registry | `webapp/var/principals.json` |
| Requester credential | `webapp/var/mac-local/credentials/akhil-local.token` |
| Dedicated models | `webapp/var/mac-local/models/` |
| Application PID and log | `webapp/var/server.pid`, `webapp/var/server.log` |
| Dedicated Ollama PID and log | `webapp/var/mac-local/ollama.pid`, `webapp/var/mac-local/ollama.log` |

The dedicated service uses the installed
`/Applications/Ollama.app/Contents/Resources/ollama` binary with cloud features
disabled, a 16,384-token context, one parallel request and one loaded model. It
uses port **11435**, leaving an existing Ollama service on **11434** alone.
The model service starts with `/` as its working directory; the controller uses
the repository directory and its `.venv` interpreter.

The launcher requires the existing configuration, model and credential files.
It does not download software, initialize or rotate credentials, rewrite
configuration, repair permissions, or migrate saved plans. Keep credential files
private to your Mac user (mode `0600`). Do not commit or share them.

The previous shared lab token cannot own assistant conversations. Individual
authentication binds **Acting as** to the signed-in identity. Separate approver
and operator credentials exercise the native separation of duties; changing the
text in an actor field cannot impersonate another principal. These are local
test accounts controlled by one Mac user, so they do not establish independent
human approvals. Customer identity provider selection remains independent of
this local setup. The initial local credentials expire on **October 17, 2026**;
their current expiry is recorded in `webapp/var/principals.json`.

## Troubleshooting

- If a port is occupied by an unverified process, inspect its PID and command
  before changing anything. Do not delete PID files to bypass the check.
- If the app opens but authentication fails, copy the requester credential again
  with `--copy-token` and replace the old value in **Session → API token**.
- If the configured model is missing, complete model setup before relaunching.
  The launcher deliberately does not choose or download a substitute model.
- If a service exits or stays unready, inspect its log listed above. The launcher
  leaves a still-running process intact for inspection.

Existing evidence and plans retain their actual states. Running locally does not
provide live-lab or production approval, validate backup restores, or resolve a
paused plan's database-side blockers. Remote deployment remains a separate step
using [REMOTE_DEPLOYMENT.md](REMOTE_DEPLOYMENT.md) and
[ORACLE_LINUX_DEPLOYMENT.md](ORACLE_LINUX_DEPLOYMENT.md).
