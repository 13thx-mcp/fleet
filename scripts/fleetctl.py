#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FLEET_DIR = Path(__file__).resolve().parents[1]
FLEET_CONFIG = FLEET_DIR / "fleet.toml"


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def run(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def component_path(component: dict[str, Any]) -> Path:
    return (FLEET_DIR / component["path"]).resolve()


def cargo_version(path: Path) -> str | None:
    manifest = path / "Cargo.toml"
    if not manifest.is_file():
        return None
    try:
        return load_toml(manifest).get("package", {}).get("version")
    except (OSError, tomllib.TOMLDecodeError):
        return None


def git_info(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "is_git": False,
        "commit": None,
        "branch": None,
        "dirty": None,
        "remote": None,
    }
    code, _, _ = run(["git", "rev-parse", "--is-inside-work-tree"], path)
    if code != 0:
        return result
    result["is_git"] = True
    code, out, _ = run(["git", "rev-parse", "HEAD"], path)
    if code == 0:
        result["commit"] = out
    code, out, _ = run(["git", "branch", "--show-current"], path)
    if code == 0:
        result["branch"] = out or "(detached)"
    code, out, _ = run(["git", "status", "--porcelain"], path)
    if code == 0:
        result["dirty"] = bool(out)
    code, out, _ = run(["git", "remote", "get-url", "origin"], path)
    if code == 0:
        result["remote"] = out
    return result


def bundle_info(path: Path, component: dict[str, Any]) -> dict[str, Any]:
    manifest_path = path / component["manifest"]
    info: dict[str, Any] = {"version": None, "release_commit": None}
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text())
            info["version"] = data.get("version")
            info["release_commit"] = data.get("release_commit")
        except (OSError, json.JSONDecodeError):
            pass
    return info


def collect(host_name: str) -> dict[str, Any]:
    fleet = load_toml(FLEET_CONFIG)
    host_path = FLEET_DIR / "hosts" / f"{host_name}.toml"
    host = load_toml(host_path)
    components: dict[str, Any] = {}

    for name, component in fleet.get("components", {}).items():
        path = component_path(component)
        entry: dict[str, Any] = {
            "kind": component["kind"],
            "path": str(path),
            "exists": path.is_dir(),
            "required": bool(component.get("required", False)),
            "binary_exists": (path / component["binary"]).is_file(),
        }
        if component["kind"] == "git" and path.is_dir():
            entry.update(git_info(path))
            entry["version"] = cargo_version(path)
        elif component["kind"] == "bundle" and path.is_dir():
            entry.update(bundle_info(path, component))
            local_config = component.get("local_config")
            entry["local_config_exists"] = bool(local_config and (path / local_config).is_file())
        components[name] = entry

    return {
        "schema_version": 1,
        "fleet_name": fleet["fleet_name"],
        "host_id": host["host_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": fleet.get("policy", {}),
        "components": components,
    }


def print_status(snapshot: dict[str, Any]) -> None:
    print(f"fleet={snapshot['fleet_name']} host={snapshot['host_id']}")
    print(f"{'component':<15} {'kind':<7} {'version':<10} {'commit':<12} {'branch':<14} {'dirty':<6} {'remote':<7} {'binary':<6}")
    for name, entry in snapshot["components"].items():
        commit = entry.get("commit")
        short_commit = commit[:10] if commit else "-"
        version = entry.get("version") or "-"
        branch = entry.get("branch") or "-"
        dirty = "yes" if entry.get("dirty") else ("no" if entry.get("dirty") is False else "-")
        remote = "yes" if entry.get("remote") else "no"
        binary = "yes" if entry.get("binary_exists") else "no"
        print(f"{name:<15} {entry['kind']:<7} {version:<10} {short_commit:<12} {branch:<14} {dirty:<6} {remote:<7} {binary:<6}")


def doctor(snapshot: dict[str, Any], require_remotes: bool) -> int:
    errors: list[str] = []
    warnings: list[str] = []
    for name, entry in snapshot["components"].items():
        if entry["required"] and not entry["exists"]:
            errors.append(f"{name}: required component directory is missing")
            continue
        if entry["kind"] == "git":
            if not entry.get("is_git"):
                errors.append(f"{name}: source component is not a Git repository")
            if entry.get("dirty"):
                warnings.append(f"{name}: working tree is dirty; automatic update must remain blocked")
            if not entry.get("remote"):
                message = f"{name}: origin remote is not configured"
                (errors if require_remotes else warnings).append(message)
        if not entry.get("binary_exists"):
            warnings.append(f"{name}: release/runtime binary is missing")
        if entry["kind"] == "bundle" and not entry.get("version"):
            errors.append(f"{name}: bundle manifest/version could not be read")

    for line in errors:
        print(f"ERROR: {line}")
    for line in warnings:
        print(f"WARN:  {line}")
    if errors:
        print(f"doctor: failed with {len(errors)} error(s), {len(warnings)} warning(s)")
        return 2
    print(f"doctor: ok with {len(warnings)} warning(s)")
    return 0


def yaml_string(value: str) -> str:
    return json.dumps(value)


def render_server(name: str, server: dict[str, Any], host: dict[str, Any], fleet: dict[str, Any]) -> str:
    component = fleet["components"][name]
    command = Path(host["mcp_root"]) / name / component["binary"]
    args = ["--root", host["workspace_root"]]
    args.extend(server.get("extra_args", []))
    lines = [
        f"name: {name}",
        f"enabled: {'true' if server.get('enabled', True) else 'false'}",
        f"command: {yaml_string(str(command))}",
        "args:",
    ]
    lines.extend(f"  - {yaml_string(str(arg))}" for arg in args)
    env = server.get("env", {})
    if env:
        lines.append("env:")
        for key in sorted(env):
            lines.append(f"  {key}: {yaml_string(str(env[key]))}")
    allowlist = server.get("tool_allowlist", [])
    if allowlist:
        lines.append("tool_allowlist:")
        lines.extend(f"  - {item}" for item in allowlist)
    lines.extend([
        f"timeout_ms: {int(server.get('timeout_ms', 30000))}",
        "restart:",
        "  policy: on-failure",
        "",
    ])
    return "\n".join(lines)


def gateway_outputs(host_name: str) -> dict[Path, str]:
    fleet = load_toml(FLEET_CONFIG)
    host = load_toml(FLEET_DIR / "hosts" / f"{host_name}.toml")
    server_dir = (FLEET_DIR / host["gateway"]["server_dir"]).resolve()
    outputs: dict[Path, str] = {}
    for name in ("filesystem", "git", "exec"):
        outputs[server_dir / f"{name}.yaml"] = render_server(name, host["servers"][name], host, fleet)
    return outputs


def render_gateway(host_name: str, check: bool) -> int:
    mismatches: list[Path] = []
    for path, desired in gateway_outputs(host_name).items():
        current = path.read_text() if path.is_file() else None
        if current != desired:
            mismatches.append(path)
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
                try:
                    with os.fdopen(fd, "w") as handle:
                        handle.write(desired)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temp_name, path)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
    if check:
        if mismatches:
            for path in mismatches:
                print(f"OUT-OF-SYNC: {path}")
            return 1
        print("gateway config: synchronized")
        return 0
    if mismatches:
        for path in mismatches:
            print(f"UPDATED: {path}")
    else:
        print("gateway config: already synchronized")
    return 0


def snapshot(host_name: str) -> int:
    data = collect(host_name)
    out = FLEET_DIR / "state" / f"{host_name}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_suffix(".json.tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(temp, out)
    print(out)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MCP fleet foundation tool")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "doctor", "snapshot"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--host", required=True)
    doctor_parser = sub.choices["doctor"]
    doctor_parser.add_argument("--require-remotes", action="store_true")
    render = sub.add_parser("render-gateway")
    render.add_argument("--host", required=True)
    render.add_argument("--check", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "status":
            print_status(collect(args.host))
            return 0
        if args.command == "doctor":
            return doctor(collect(args.host), args.require_remotes)
        if args.command == "snapshot":
            return snapshot(args.host)
        if args.command == "render-gateway":
            return render_gateway(args.host, args.check)
    except (OSError, KeyError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        print(f"fleetctl: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
