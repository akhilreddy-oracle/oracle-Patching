# Production Certification Gate (S13)

Production mutation remains outside the certified build until the full S13
checklist in `IMPLEMENTATION_PLAN.md` is completed. This document describes the
**fail-closed switch** plus the **lab stubs** that now exist.

## Switch

| Variable | Meaning |
| --- | --- |
| `OPU_PRODUCTION_MODE=1` | Enable the production gate |
| `OPU_PRODUCTION_CERT_FILE` | Optional path to the marker (default `/etc/oracle-patching/production.cert`; webapp uses `webapp/var/production.cert`) |

The marker must be a regular file, owned by root or the effective service user,
with a single hard link and no group/other write permission (for example, mode
`0600`). Symlinks, special files, invalid UTF-8 and files larger than 16 KiB are
rejected. Both native and webapp guards read one descriptor and reject a file
that changes during that read.

Each required switch must occur exactly once on its own line; contradictory or
duplicate assignments are invalid. The minimum content is:

```text
OPU_PRODUCTION_CERTIFIED=1
```

When production mode is off (the lab default), the gate is a no-op.

The marker is a local administrator-controlled admission switch. Its presence
does not verify a release signature or constitute independent production
approval; those checks and sign-off remain separate requirements.

## What is blocked

- `opu-patch-plan authorize` and `dispatch`
- Webapp live (non-TEST_MODE) SSH execute via `planctl.execute_next_task`

TEST_MODE fixture demos remain available for development.

## Lab stubs available now

| Stub | How |
| --- | --- |
| HTTPS for webapp | Set `OPU_WEBAPP_TLS_CERT` and `OPU_WEBAPP_TLS_KEY` before `python3 webapp/server.py` |
| SBOM | `scripts/generate_sbom.sh [dist/sbom.json]` |
| Detached release signing | `scripts/sign_release.sh [dist]` then `scripts/verify_release.sh dist lab-signing.pub` |
| Cert verify | `scripts/verify_production_cert.sh` |
| Expanded checklist | Set `OPU_PRODUCTION_REQUIRE_CHECKLIST=1` and include `OPU_SBOM_VERIFIED=1`, `OPU_RELEASE_SIGNED=1`, `OPU_THREAT_MODEL_SIGNED=1` in the marker |

These are development/lab helpers. They do not replace enterprise mTLS for
agents, HA control-plane topology, secret-manager integration, vulnerability
CI, or pilot sign-off.
