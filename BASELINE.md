# Fleet runtime-host configuration baseline

Date: 2026-09-22

## Deployment freeze

This remediation makes no deployment, restart, push, tag deletion, history rewrite, or
credential rotation. Production runtime files are read only during baseline collection.

## Repository state observed

- Fleet source worktree: `feature/sonarqube-main-gate` at `79c2814`, dirty in
  `hosts/aira.toml`, `scripts/fleetctl.py`, and `tests/test_fleetctl.py`; preserved.
- Studio source worktree: `main` at `c360a24`, dirty; preserved.
- Gateway source worktree: `feature/m7-protocol-spike` at `062f264`, dirty; preserved.
- Filesystem: `main` at `ec68a04`, clean.
- Git MCP: `main` at `31ceadd`, clean.
- Fleet remediation worktree: `remediation/fleet-runtime-config`, clean branch from
  Fleet `main` at `16d82dd`.

## Fleet release references

| Tag | Peeled commit |
| --- | --- |
| `v0.3.0` | `7bad5e2` |
| `v0.3.1` | `bd9d973` |
| `v0.3.2` | `16d82dd` |

## Host-profile inventory

- Source authority before remediation: tracked `hosts/aira.toml`.
- Active runtime candidate: `mcp-server/runtime/fleet/hosts/aira.toml` (outside the
  Fleet Git repository).
- Runtime profile metadata: regular file, mode `0644`, SHA-256
  `96f3fbe52f95466f40426f0da3f98f93279f4b2b084d63c0336fa8352074d2ec`.
- The runtime profile has every source-profile structural key plus
  `tunnel.control_plane_api_key_file`. Values were not copied into this note.
- The referenced credential file was not opened or read. Its value is not present in
  this repository baseline.

The runtime profile has sufficient configuration shape for lossless migration: schema
version, host identity, all roots, tunnel identity, tunnel secret-file reference,
gateway settings, and all enabled-server settings are present. Value preservation is
verified later through a source-independent render plan without printing the profile.
