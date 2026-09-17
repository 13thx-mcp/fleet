# MCP Fleet Foundation

Local fleet metadata and safety tooling for keeping the MCP stack consistent across Aira and Mirin.

## Scope

This directory is the control-plane source for **desired component identity and host-specific rendering**, not a runtime state store. It intentionally does not contain tunnel credentials, PIDs, logs, build outputs, or node_modules.

Current Phase 1 responsibilities:

- declare required MCP components and their build/test commands in `fleet.toml`;
- keep host-local absolute paths in `hosts/<host>.toml`;
- render Gateway `servers.d/*.yaml` deterministically from the host profile;
- report Git commit/branch/dirty/remote state for every source component;
- report tunnel bundle version without copying the runtime binary or `config.yaml`;
- emit a local JSON snapshot under `state/` for later Studio integration.

Automatic pull/build/restart remains disabled until every source component has a configured remote and a clean fast-forward-only update path.

## Commands

```bash
cd mcp-server/fleet
python3 scripts/fleetctl.py status --host aira
python3 scripts/fleetctl.py doctor --host aira
python3 scripts/fleetctl.py render-gateway --host aira --check
python3 scripts/fleetctl.py render-gateway --host aira
python3 scripts/fleetctl.py snapshot --host aira
```

`render-gateway --check` never writes files. The write form updates only the managed `filesystem.yaml`, `git.yaml`, and `exec.yaml` files by atomic replacement.

## Synchronization boundary

Changes become eligible for cross-host synchronization only after they are committed and pushed to a configured Git remote. Local uncommitted files are never treated as fleet desired state.

## Secrets and local state

Do not commit:

- `tunnel-client/config.yaml` or tunnel credentials;
- Studio `data/` and registry runtime state;
- build output (`target/`, `node_modules/`);
- PID/socket/log files;
- generated `fleet/state/*` snapshots.

The tunnel runtime is represented by its checked manifest/version, not by copying the 58 MB local bundle into this fleet repo.
