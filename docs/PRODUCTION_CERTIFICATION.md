# Production Certification Gate (S13)

Production mutation remains outside the certified build until the full S13
checklist in `IMPLEMENTATION_PLAN.md` is completed. This document describes the
**fail-closed switch** plus the **lab stubs** that now exist.

## Switch

| Variable | Meaning |
| --- | --- |
| `OPU_PRODUCTION_MODE=1` | Enable the production gate |
| `OPU_PRODUCTION_CERT_FILE` | Optional path to the marker (default `/etc/oracle-patching/production.cert`; webapp uses `webapp/var/production.cert`) |

The marker file must be a regular file (not a symlink) containing:

```text
OPU_PRODUCTION_CERTIFIED=1
```

When production mode is off (the lab default), the gate is a no-op.

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
