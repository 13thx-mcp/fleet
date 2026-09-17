# MCP Fleet Foundation

Local fleet metadata and safety tooling for keeping the MCP stack consistent across Aira and Mirin.

## Scope

This directory is the control-plane source for **desired component identity and host-specific rendering**, not a runtime state store. It intentionally does not contain tunnel credentials, PIDs, logs, build outputs, or node_modules.

Current Phase 1 responsibilities:

- declare required MCP components, GitHub repositories, build outputs, and installed runtime binary names in `fleet.toml`;
- keep host-local absolute paths in `hosts/<host>.toml`;
- render Gateway `servers.d/*.yaml` deterministically from the host profile;
- report Git commit/branch/dirty/remote state for every source component;
- keep project source checkouts directly under `mcp-server/<project>`; keep only flat Rust MCP executables in `mcp-server/bin`; keep non-MCP runtime/config/state under `mcp-server/runtime`;
- report tunnel bundle version without copying `config.yaml` into source control;
- emit a local JSON snapshot under `state/` for later Studio integration.

Automatic pull/build/restart remains disabled until every source component has a configured remote and a clean fast-forward-only update path.

## Commands

```bash
cd mcp-server/src/fleet
python3 scripts/fleetctl.py status --host aira
python3 scripts/fleetctl.py doctor --host aira
python3 scripts/fleetctl.py render-gateway --host aira --check
python3 scripts/fleetctl.py render-gateway --host aira
python3 scripts/fleetctl.py render-studio --host aira
python3 scripts/fleetctl.py render-tunnel --host aira
python3 scripts/fleetctl.py deploy-control --host aira
python3 scripts/fleetctl.py install --host aira --component filesystem
python3 scripts/fleetctl.py tunnel-check --host aira
python3 scripts/fleetctl.py tunnel-update --host aira
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

## Official tunnel-client updates

`tunnel-client` is not forked or rebuilt by this fleet. Its source of truth is the official OpenAI repository and GitHub Releases:

- repository: `https://github.com/openai/tunnel-client`
- release API: `https://api.github.com/repos/openai/tunnel-client/releases/latest`
- selected artifact: `tunnel-client-runtime-cloudflared-v<version>-<os>-<arch>.zip`

`tunnel-check` detects the local OS/architecture and compares the installed binary's `--version` against the latest official release. `tunnel-update` downloads the matching official asset plus `SHA256SUMS.txt`, verifies SHA-256, validates the extracted binary version, installs it under `bin/tunnel-client/releases/v<version>`, and atomically repoints `bin/tunnel-client/current`. The host-local `config.yaml` is outside the release directory and is never overwritten.

The installed layout is:

```text
mcp-server/bin/tunnel-client/
├── config.yaml          # host-local, never overwritten
├── current -> releases/vX.Y.Z
└── releases/
    └── vX.Y.Z/          # verified official release contents
```

## Source/runtime separation

Projects live directly under `mcp-server/<project>`. `mcp-server/bin` is reserved for flat Rust MCP executables only; non-MCP runtime/config/state lives under `mcp-server/runtime`. Gateway and Studio must execute Rust MCPs from `bin`, never `target/release` directly.

Project-owned source repositories are hosted under the private GitHub organization `13thx-mcp`. `tunnel-client` is the exception: it tracks the official `openai/tunnel-client` releases directly.

## Runtime-only control bundle

`deploy-control` copies the fleet manifest, selected host profile, documentation, and `fleetctl.py` into `mcp-server/runtime/fleet`. Source repositories are resolved from `host.source_root`; Rust MCP binaries are installed into the flat `host.bin_root`; non-MCP services such as Studio use `install_scope = "runtime"` and install beneath `host.runtime_root`. Generated operational state remains under `host.runtime_root`.

Runtime configuration is generated outside source repositories:

```text
mcp-server/
├── bin/
│   ├── rust-mcp-filesystem
│   ├── rust-mcp-git
│   ├── rust-mcp-exec
│   └── rust-mcp-gateway
└── runtime/
    ├── fleet/
    ├── gateway/servers.d/
    ├── studio/
    └── tunnel-client/
        ├── config.yaml
        ├── current -> releases/vX.Y.Z
        └── releases/
```
