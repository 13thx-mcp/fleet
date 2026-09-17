#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
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


def parse_semver(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(?:^|\s|v)(\d+)\.(\d+)\.(\d+)(?:\s|$)", text)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def binary_version(binary: Path) -> str | None:
    if not binary.is_file():
        return None
    code, out, _ = run([str(binary), "--version"], binary.parent)
    if code != 0:
        return None
    parsed = parse_semver(out)
    return ".".join(str(part) for part in parsed) if parsed else None


def bundle_info(path: Path, component: dict[str, Any]) -> dict[str, Any]:
    current = path / "current"
    binary = (current / component["binary"]) if current.exists() else (path / component["binary"])
    manifest_path = (current / component["manifest"]) if current.exists() else (path / component["manifest"])
    info: dict[str, Any] = {
        "version": binary_version(binary),
        "release_commit": None,
        "upstream_cloudflared_version": None,
    }
    if manifest_path.is_file():
        try:
            data = json.loads(manifest_path.read_text())
            info["upstream_cloudflared_version"] = data.get("version")
            info["release_commit"] = data.get("release_commit")
        except (OSError, json.JSONDecodeError):
            pass
    return info


def normalize_platform(system: str, machine: str) -> tuple[str, str]:
    os_name = system.strip().lower()
    if os_name == "darwin":
        target_os = "darwin"
    elif os_name == "linux":
        target_os = "linux"
    elif os_name == "windows":
        target_os = "windows"
    else:
        raise RuntimeError(f"unsupported OS: {system}")

    arch_name = machine.strip().lower()
    if arch_name in {"x86_64", "amd64"}:
        target_arch = "amd64"
    elif arch_name in {"arm64", "aarch64"}:
        target_arch = "arm64"
    else:
        raise RuntimeError(f"unsupported architecture: {machine}")
    return target_os, target_arch


def host_platform() -> tuple[str, str]:
    return normalize_platform(platform.system(), platform.machine())


def http_bytes(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "13thx-mcp-fleetctl",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def latest_release(component: dict[str, Any]) -> dict[str, Any]:
    return json.loads(http_bytes(component["release_api"]))


def release_assets(release: dict[str, Any]) -> dict[str, str]:
    return {asset["name"]: asset["browser_download_url"] for asset in release.get("assets", [])}


def expected_checksum(checksums: str, filename: str) -> str:
    for line in checksums.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            return parts[0].lower()
    raise RuntimeError(f"checksum entry not found for {filename}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tunnel_release_state(host_name: str) -> dict[str, Any]:
    fleet = load_toml(FLEET_CONFIG)
    host = load_toml(FLEET_DIR / "hosts" / f"{host_name}.toml")
    component = fleet["components"]["tunnel-client"]
    install_dir = Path(host["runtime_root"]).resolve() / component.get("install_dir", "tunnel-client")
    info = bundle_info(install_dir, component) if install_dir.is_dir() else {"version": None}
    release = latest_release(component)
    tag = release["tag_name"]
    latest_version = tag.removeprefix("v")
    target_os, target_arch = host_platform()
    asset_name = f"{component['asset_prefix']}-v{latest_version}-{target_os}-{target_arch}.zip"
    assets = release_assets(release)
    if asset_name not in assets:
        raise RuntimeError(f"official release {tag} has no asset {asset_name}")
    if "SHA256SUMS.txt" not in assets:
        raise RuntimeError(f"official release {tag} has no SHA256SUMS.txt")
    current_version = info.get("version")
    current_semver = parse_semver(current_version or "")
    latest_semver = parse_semver(latest_version)
    return {
        "component": component,
        "install_dir": install_dir,
        "os": target_os,
        "arch": target_arch,
        "current_version": current_version,
        "latest_version": latest_version,
        "latest_tag": tag,
        "asset_name": asset_name,
        "asset_url": assets[asset_name],
        "checksums_url": assets["SHA256SUMS.txt"],
        "up_to_date": current_semver is not None and latest_semver == current_semver,
        "update_available": current_semver is None or (latest_semver is not None and latest_semver > current_semver),
    }


def tunnel_check(host_name: str) -> int:
    state = tunnel_release_state(host_name)
    print(f"host={host_name} os={state['os']} arch={state['arch']}")
    print(f"installed={state['current_version'] or '-'} latest={state['latest_version']} asset={state['asset_name']}")
    print("status=up-to-date" if state["up_to_date"] else "status=update-available")
    return 0


def safe_extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"unsafe path in release archive: {member.filename}")
        bundle.extractall(destination)


def tunnel_update(host_name: str, force: bool) -> int:
    state = tunnel_release_state(host_name)
    if state["up_to_date"] and not force:
        print(f"tunnel-client {state['current_version']} is already current")
        return 0
    if not state["update_available"] and not force and state["current_version"]:
        raise RuntimeError(
            f"installed tunnel-client {state['current_version']} is newer than latest official {state['latest_version']}"
        )

    install_dir: Path = state["install_dir"]
    releases_dir = install_dir / "releases"
    releases_dir.mkdir(parents=True, exist_ok=True)
    version_dir = releases_dir / f"v{state['latest_version']}"

    with tempfile.TemporaryDirectory(prefix="tunnel-client-update-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        archive = temp_dir / state["asset_name"]
        archive.write_bytes(http_bytes(state["asset_url"]))
        checksum_text = http_bytes(state["checksums_url"]).decode("utf-8")
        expected = expected_checksum(checksum_text, state["asset_name"])
        actual = sha256_file(archive)
        if actual != expected:
            raise RuntimeError(f"checksum mismatch for {state['asset_name']}: expected {expected}, got {actual}")

        extracted = temp_dir / "extracted"
        extracted.mkdir()
        safe_extract_zip(archive, extracted)
        staged_binary = extracted / state["component"]["binary"]
        for executable_name in (state["component"]["binary"], "cloudflared"):
            executable = extracted / executable_name
            if executable.is_file():
                executable.chmod(executable.stat().st_mode | 0o111)
        staged_version = binary_version(staged_binary)
        if staged_version != state["latest_version"]:
            raise RuntimeError(
                f"release binary version mismatch: expected {state['latest_version']}, got {staged_version or 'unknown'}"
            )

        staged_dir = releases_dir / f".v{state['latest_version']}.{os.getpid()}.tmp"
        if staged_dir.exists():
            shutil.rmtree(staged_dir)
        shutil.copytree(extracted, staged_dir)
        for executable_name in (state["component"]["binary"], "cloudflared"):
            executable = staged_dir / executable_name
            if executable.is_file():
                executable.chmod(executable.stat().st_mode | 0o111)
        if version_dir.exists():
            shutil.rmtree(version_dir)
        os.replace(staged_dir, version_dir)

    current_link = install_dir / "current"
    temp_link = install_dir / f".current.{os.getpid()}.tmp"
    if temp_link.exists() or temp_link.is_symlink():
        temp_link.unlink()
    temp_link.symlink_to(Path("releases") / version_dir.name)
    os.replace(temp_link, current_link)
    print(
        f"UPDATED: tunnel-client {state['current_version'] or '-'} -> {state['latest_version']} "
        f"({state['os']}/{state['arch']}, sha256 verified)"
    )
    return 0


def collect(host_name: str) -> dict[str, Any]:
    fleet = load_toml(FLEET_CONFIG)
    host_path = FLEET_DIR / "hosts" / f"{host_name}.toml"
    host = load_toml(host_path)
    runtime_root = Path(host["runtime_root"]).resolve()
    components: dict[str, Any] = {}

    for name, component in fleet.get("components", {}).items():
        kind = component["kind"]
        install_dir = runtime_root / component.get("install_dir", name)
        binary_path = install_dir / component["binary"]
        if kind == "upstream_release":
            versioned_binary = install_dir / "current" / component["binary"]
            binary_path = versioned_binary if versioned_binary.exists() else binary_path
        entry: dict[str, Any] = {
            "kind": kind,
            "required": bool(component.get("required", False)),
            "repository": component.get("repository"),
            "install_dir": str(install_dir),
            "binary_path": str(binary_path),
            "binary_exists": binary_path.is_file(),
        }
        if kind == "git":
            path = component_path(component)
            entry["path"] = str(path)
            entry["source_exists"] = path.is_dir()
            build_output = component.get("build_output")
            entry["build_output_exists"] = bool(build_output and (path / build_output).is_file())
            if path.is_dir():
                entry.update(git_info(path))
                entry["version"] = cargo_version(path)
        elif kind == "upstream_release":
            entry["path"] = str(install_dir)
            entry["source_exists"] = False
            if install_dir.is_dir():
                entry.update(bundle_info(install_dir, component))
                local_config = component.get("local_config")
                entry["local_config_exists"] = bool(local_config and (install_dir / local_config).is_file())
        components[name] = entry

    return {
        "schema_version": 2,
        "fleet_name": fleet["fleet_name"],
        "host_id": host["host_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": host.get("source_root"),
        "runtime_root": str(runtime_root),
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
        if entry["required"] and not entry.get("binary_exists"):
            errors.append(f"{name}: required runtime binary is missing")
        if entry["kind"] == "git":
            if not entry.get("source_exists"):
                warnings.append(f"{name}: source checkout is absent; runtime-only mode")
                continue
            if not entry.get("is_git"):
                errors.append(f"{name}: source component is not a Git repository")
            if entry.get("dirty"):
                warnings.append(f"{name}: working tree is dirty; automatic update must remain blocked")
            if not entry.get("remote"):
                message = f"{name}: origin remote is not configured"
                (errors if require_remotes else warnings).append(message)
        if entry["kind"] == "upstream_release":
            if not entry.get("version"):
                errors.append(f"{name}: installed runtime version could not be read")
            if not entry.get("local_config_exists"):
                warnings.append(f"{name}: local config is missing")

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
    command = Path(host["runtime_root"]) / component.get("install_dir", name) / component["binary"]
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


def install_component(host_name: str, component_name: str) -> int:
    fleet = load_toml(FLEET_CONFIG)
    host = load_toml(FLEET_DIR / "hosts" / f"{host_name}.toml")
    component = fleet.get("components", {}).get(component_name)
    if component is None:
        print(f"fleetctl: unknown component {component_name}", file=sys.stderr)
        return 2
    if component.get("kind") != "git":
        print(f"fleetctl: {component_name} is not a source-built component", file=sys.stderr)
        return 2
    source = component_path(component) / component["build_output"]
    if not source.is_file():
        print(f"fleetctl: build output is missing: {source}", file=sys.stderr)
        return 2
    install_dir = Path(host["runtime_root"]).resolve() / component.get("install_dir", component_name)
    install_dir.mkdir(parents=True, exist_ok=True)
    destination = install_dir / component["binary"]
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=install_dir)
    os.close(fd)
    try:
        shutil.copy2(source, temp_name)
        os.chmod(temp_name, 0o755)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print(f"INSTALLED: {component_name} -> {destination}")
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
    install = sub.add_parser("install")
    install.add_argument("--host", required=True)
    install.add_argument("--component", required=True)
    tunnel_check_parser = sub.add_parser("tunnel-check")
    tunnel_check_parser.add_argument("--host", required=True)
    tunnel_update_parser = sub.add_parser("tunnel-update")
    tunnel_update_parser.add_argument("--host", required=True)
    tunnel_update_parser.add_argument("--force", action="store_true")
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
        if args.command == "install":
            return install_component(args.host, args.component)
        if args.command == "tunnel-check":
            return tunnel_check(args.host)
        if args.command == "tunnel-update":
            return tunnel_update(args.host, args.force)
    except (OSError, KeyError, RuntimeError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        print(f"fleetctl: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
