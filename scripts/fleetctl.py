#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

FLEET_DIR = Path(__file__).resolve().parents[1]
FLEET_CONFIG = FLEET_DIR / "fleet.toml"
TUNNEL_ID_RE = re.compile(r"^tunnel_[0-9a-f]{32}$")


def load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def run(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    proc = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def component_path(component: dict[str, Any], host: dict[str, Any]) -> Path:
    return (Path(host["source_root"]) / component["source_dir"]).resolve()


def component_install_dir(name: str, component: dict[str, Any], host: dict[str, Any]) -> Path:
    default_scope = "bin" if component.get("kind") == "git" else "runtime"
    scope = component.get("install_scope", default_scope)
    if scope == "bin":
        return Path(host["bin_root"]).resolve()
    if scope == "runtime":
        return Path(host["runtime_root"]).resolve() / component.get("install_dir", name)
    raise RuntimeError(f"unsupported install_scope for {name}: {scope}")


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


def tunnel_binary_identity(binary: Path, role: str) -> tuple[str, str]:
    if not binary.is_file():
        raise RuntimeError(f"missing staged {role} tunnel binary")
    code, out, _ = run([str(binary), "--version"], binary.parent)
    if code != 0:
        raise RuntimeError(f"cannot query staged {role} tunnel binary version")
    if role == "runtime":
        match = re.search(r"^(\d+\.\d+\.\d+) git sha: ([0-9a-f]{7,64})\b", out)
    else:
        match = re.search(r"^(\d+\.\d+\.\d+)\+([0-9a-f]{7,64})\b", out)
    if not match:
        raise RuntimeError(f"staged {role} tunnel binary identity is invalid")
    return match.group(1), match.group(2)


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


def select_tunnel_release_assets(
    assets: dict[str, str], version: str, target_os: str, target_arch: str, runtime_prefix: str
) -> tuple[str, str]:
    runtime_name = f"{runtime_prefix}-v{version}-{target_os}-{target_arch}.zip"
    full_name = f"tunnel-client-v{version}-{target_os}-{target_arch}.zip"
    for name in (runtime_name, full_name):
        if name not in assets:
            raise RuntimeError(f"official release has no asset {name}")
    return runtime_name, full_name


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
    assets = release_assets(release)
    asset_name, full_asset_name = select_tunnel_release_assets(
        assets, latest_version, target_os, target_arch, component["asset_prefix"]
    )
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
        "full_asset_name": full_asset_name,
        "full_asset_url": assets[full_asset_name],
        "checksums_url": assets["SHA256SUMS.txt"],
        "up_to_date": current_semver is not None and latest_semver == current_semver,
        "update_available": current_semver is None or (latest_semver is not None and latest_semver > current_semver),
    }


def tunnel_check(host_name: str) -> int:
    state = tunnel_release_state(host_name)
    print(f"host={host_name} os={state['os']} arch={state['arch']}")
    print(f"installed={state['current_version'] or '-'} latest={state['latest_version']} runtime={state['asset_name']} full={state['full_asset_name']}")
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
        full_archive = temp_dir / state["full_asset_name"]
        archive.write_bytes(http_bytes(state["asset_url"]))
        full_archive.write_bytes(http_bytes(state["full_asset_url"]))
        checksum_text = http_bytes(state["checksums_url"]).decode("utf-8")
        for name, candidate in ((state["asset_name"], archive), (state["full_asset_name"], full_archive)):
            expected = expected_checksum(checksum_text, name)
            actual = sha256_file(candidate)
            if actual != expected:
                raise RuntimeError(f"checksum mismatch for {name}: expected {expected}, got {actual}")

        extracted = temp_dir / "extracted"
        full_extracted = temp_dir / "full-extracted"
        extracted.mkdir()
        full_extracted.mkdir()
        safe_extract_zip(archive, extracted)
        safe_extract_zip(full_archive, full_extracted)
        staged_binary = extracted / state["component"]["binary"]
        staged_full_binary = full_extracted / "tunnel-client"
        for executable_name in (state["component"]["binary"], "cloudflared"):
            executable = extracted / executable_name
            if executable.is_file():
                executable.chmod(executable.stat().st_mode | 0o111)
        staged_full_binary.chmod(staged_full_binary.stat().st_mode | 0o111)
        runtime_version, runtime_commit = tunnel_binary_identity(staged_binary, "runtime")
        full_version, full_commit = tunnel_binary_identity(staged_full_binary, "full")
        if runtime_version != state["latest_version"] or full_version != state["latest_version"]:
            raise RuntimeError(
                f"release binary version mismatch: expected {state['latest_version']}, got runtime={runtime_version} full={full_version}"
            )
        if runtime_commit != full_commit:
            raise RuntimeError("full and runtime tunnel artifacts have different release commits")

        staged_dir = releases_dir / f".v{state['latest_version']}.{os.getpid()}.tmp"
        if staged_dir.exists():
            shutil.rmtree(staged_dir)
        shutil.copytree(extracted, staged_dir)
        shutil.copy2(staged_full_binary, staged_dir / "tunnel-client")
        (staged_dir / "tunnel-client").chmod(staged_full_binary.stat().st_mode)
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
    bin_root = Path(host["bin_root"]).resolve()
    runtime_root = Path(host["runtime_root"]).resolve()
    components: dict[str, Any] = {}

    for name, component in fleet.get("components", {}).items():
        kind = component["kind"]
        install_dir = component_install_dir(name, component, host)
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
            path = component_path(component, host)
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
        "bin_root": str(bin_root),
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


def gateway_policy_text(host: dict[str, Any]) -> str | None:
    if int(host.get("schema_version", 1)) < 2:
        return None
    policy = host.get("gateway", {}).get("policy", {})
    profiles = policy.get("profiles", ["inspect", "develop", "release", "ops", "hardware"])
    if not isinstance(profiles, list) or not profiles or any(not isinstance(name, str) or not name for name in profiles):
        raise RuntimeError("gateway.policy.profiles must be a non-empty string list")
    active_profile = policy.get("active_profile", "develop")
    if active_profile not in profiles:
        raise RuntimeError("gateway.policy.active_profile must be declared")
    limits = {
        "global_active": int(policy.get("global_active", 16)),
        "global_queue": int(policy.get("global_queue", 64)),
        "queue_wait_ms": int(policy.get("queue_wait_ms", 30000)),
        "default_child_active": int(policy.get("default_child_active", 4)),
    }
    if any(value <= 0 for value in limits.values()):
        raise RuntimeError("gateway.policy limits must be positive")
    drain_deadline_ms = int(policy.get("drain_deadline_ms", 60000))
    if drain_deadline_ms <= 0:
        raise RuntimeError("gateway.policy.drain_deadline_ms must be positive")
    lines = [
        "schema_version: 1",
        f"active_profile: {yaml_string(active_profile)}",
        "limits:",
        *(f"  {key}: {value}" for key, value in limits.items()),
        "tool_class_defaults:",
        "  read: {concurrency: 4}",
        "  mutation: {concurrency: 1}",
        "  long-running: {concurrency: 1}",
        "  control: {concurrency: 1}",
        "drain:",
        f"  deadline_ms: {drain_deadline_ms}",
        f"  allow_safe_reads: {'true' if policy.get('allow_safe_reads', False) else 'false'}",
        "payload:",
        "  request_bytes: 1048576",
        "  response_bytes: 2097152",
        "  text_preview_bytes: 65536",
        "  structured_bytes: 1048576",
        "  binary_bytes: 1048576",
        "artifacts:",
        "  enabled: true",
        "  ttl_seconds: 900",
        "  max_item_bytes: 8388608",
        "  max_total_bytes: 67108864",
        "profiles:",
        *(f"  {name}: {{}}" for name in sorted(profiles)),
        "children: {}",
        "",
    ]
    return "\n".join(lines)


def gateway_policy_path(host: dict[str, Any]) -> Path:
    runtime_root = Path(host["runtime_root"]).resolve()
    relative = Path(host.get("gateway", {}).get("policy_file", "gateway/gateway.yaml"))
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("gateway.policy_file must be runtime-root relative")
    return (runtime_root / relative).resolve()


def render_server(name: str, server: dict[str, Any], host: dict[str, Any], fleet: dict[str, Any]) -> str:
    component = fleet["components"][name]
    command = Path(host["bin_root"]) / component["binary"]
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
    server_dir = (Path(host["runtime_root"]) / host["gateway"]["server_dir"]).resolve()
    outputs: dict[Path, str] = {}
    for name in ("filesystem", "git", "exec"):
        outputs[server_dir / f"{name}.yaml"] = render_server(name, host["servers"][name], host, fleet)
    policy = gateway_policy_text(host)
    if policy is not None:
        outputs[gateway_policy_path(host)] = policy
    return outputs


def render_plan_data(host: dict[str, Any], fleet: dict[str, Any]) -> dict[str, Any]:
    runtime_root = Path(host["runtime_root"]).resolve()
    outputs: list[dict[str, Any]] = []

    def add(surface: str, destination: Path, content: str, effects: list[str]) -> None:
        resolved = destination.resolve()
        try:
            relative = resolved.relative_to(runtime_root)
        except ValueError as exc:
            raise RuntimeError(f"render-plan destination escapes runtime_root: {resolved}") from exc
        encoded = content.encode("utf-8")
        outputs.append({
            "surface": surface,
            "relative_path": relative.as_posix(),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "content_encoding": "base64",
            "content_b64": base64.b64encode(encoded).decode("ascii"),
            "ownership": "fleet_managed",
            "effects": effects,
        })

    server_dir = (runtime_root / host["gateway"]["server_dir"]).resolve()
    for name in ("filesystem", "git", "exec"):
        add(
            f"gateway.{name}",
            server_dir / f"{name}.yaml",
            render_server(name, host["servers"][name], host, fleet),
            ["gateway_reload"],
        )

    policy = gateway_policy_text(host)
    if policy is not None:
        add("gateway.policy", gateway_policy_path(host), policy, ["gateway_reload"])

    add(
        "studio.config",
        runtime_root / "studio" / "studio.toml",
        studio_config_text(host),
        ["studio_restart"],
    )
    add(
        "tunnel.config",
        runtime_root / "tunnel-client" / "config.yaml",
        tunnel_config_text(host),
        ["tunnel_restart"],
    )

    outputs.sort(key=lambda item: (item["relative_path"], item["surface"]))
    return {
        "schema_version": 1,
        "host_id": host["host_id"],
        "runtime_root": str(runtime_root),
        "outputs": outputs,
    }


def render_plan(host_name: str, json_output: bool) -> int:
    if not json_output:
        raise RuntimeError("render-plan currently requires --json")
    fleet = load_toml(FLEET_CONFIG)
    host = load_toml(FLEET_DIR / "hosts" / f"{host_name}.toml")
    print(json.dumps(render_plan_data(host, fleet), sort_keys=True, separators=(",", ":")))
    return 0


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


def toml_string(value: str) -> str:
    return json.dumps(value)


def toml_array(values: list[str]) -> str:
    return "[" + ", ".join(toml_string(value) for value in values) + "]"


def studio_config_text(host: dict[str, Any]) -> str:
    bin_root = Path(host["bin_root"]).resolve()
    runtime_root = Path(host["runtime_root"]).resolve()
    workspace_root = host["workspace_root"]
    lines = [
        "log_capacity = 500",
        "stop_timeout_ms = 3000",
        "",
        "[server]",
        f"listen_addr = {toml_string(host.get('studio', {}).get('listen_addr', '127.0.0.1:18100'))}",
        "",
        "[registry]",
        f"path = {toml_string(str(runtime_root / 'studio' / 'data' / 'registry.toml'))}",
        f"mcp_root = {toml_string(str(bin_root))}",
        "",
        "[tunnel]",
        "name = \"Secure tunnel\"",
        f"runtime = {toml_string(str(runtime_root / 'tunnel-client' / 'current' / 'tunnel-client-runtime-cloudflared'))}",
        f"working_dir = {toml_string(str(runtime_root / 'tunnel-client'))}",
        f"config_file = {toml_string(str(runtime_root / 'tunnel-client' / 'config.yaml'))}",
        "",
    ]
    binary_names = {"filesystem": "rust-mcp-filesystem", "git": "rust-mcp-git", "exec": "rust-mcp-exec"}
    display_names = {"filesystem": "Filesystem", "git": "Git", "exec": "Exec"}
    for name in ("filesystem", "git", "exec"):
        server = host["servers"][name]
        args = ["--root", workspace_root]
        args.extend(str(item) for item in server.get("extra_args", []))
        lines.extend([
            f"[mcp.{name}]",
            f"name = {toml_string(display_names[name])}",
            f"command = {toml_string(str(bin_root / binary_names[name]))}",
            f"working_dir = {toml_string(str(bin_root))}",
            f"args = {toml_array(args)}",
        ])
        env = server.get("env", {})
        if env:
            lines.append(f"[mcp.{name}.env]")
            for key in sorted(env):
                lines.append(f"{key} = {toml_string(str(env[key]))}")
        lines.append("")
    return "\n".join(lines)


def render_studio(host_name: str, check: bool) -> int:
    host = load_toml(FLEET_DIR / "hosts" / f"{host_name}.toml")
    output = Path(host["runtime_root"]).resolve() / "studio" / "studio.toml"
    desired = studio_config_text(host)
    current = output.read_text() if output.is_file() else None
    if current == desired:
        print("studio config: synchronized")
        return 0
    if check:
        print(f"OUT-OF-SYNC: {output}")
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(desired)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print(f"UPDATED: {output}")
    return 0


def tunnel_config_text(host: dict[str, Any]) -> str:
    bin_root = Path(host["bin_root"]).resolve()
    runtime_root = Path(host["runtime_root"]).resolve()
    gateway = bin_root / "rust-mcp-gateway"
    server_dir = runtime_root / "gateway" / "servers.d"

    tunnel = host.get("tunnel", {})
    tunnel_id = tunnel.get("tunnel_id") if isinstance(tunnel, dict) else None
    if not isinstance(tunnel_id, str) or not TUNNEL_ID_RE.fullmatch(tunnel_id):
        raise RuntimeError("invalid or missing tunnel.tunnel_id")

    lines = [
        "config_version: 1", "",
        "control_plane:", f"  tunnel_id: {yaml_string(tunnel_id)}", "  base_url: \"https://api.openai.com\"", "  poll_channels:", "    - main", "",
        "health:", "  listen_addr: \"127.0.0.1:18080\"", "",
        "admin_ui:", "  open_browser: false", "",
        "log:", "  level: \"info\"", "  format: \"struct-text\"", "",
        "mcp:", "  commands:", "    - channel: main",
        f"      command: \"{gateway} --config-dir {server_dir}\"",
        "",
    ]
    return "\n".join(lines)


def render_tunnel_config(host_name: str, check: bool) -> int:
    host = load_toml(FLEET_DIR / "hosts" / f"{host_name}.toml")
    output = Path(host["runtime_root"]).resolve() / "tunnel-client" / "config.yaml"
    desired = tunnel_config_text(host)
    current = output.read_text() if output.is_file() else None
    if current == desired:
        print("tunnel config: synchronized")
        return 0
    if check:
        print(f"OUT-OF-SYNC: {output}")
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(desired)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, output)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print(f"UPDATED: {output}")
    return 0


def deploy_control(host_name: str) -> int:
    host_path = FLEET_DIR / "hosts" / f"{host_name}.toml"
    host = load_toml(host_path)
    destination = Path(host["runtime_root"]).resolve() / "fleet"
    (destination / "scripts").mkdir(parents=True, exist_ok=True)
    (destination / "hosts").mkdir(parents=True, exist_ok=True)
    copies = [
        (FLEET_CONFIG, destination / "fleet.toml"),
        (Path(__file__).resolve(), destination / "scripts" / "fleetctl.py"),
        (host_path, destination / "hosts" / host_path.name),
        (FLEET_DIR / "README.md", destination / "README.md"),
    ]
    for source, target in copies:
        shutil.copy2(source, target)
    (destination / "scripts" / "fleetctl.py").chmod(0o755)
    print(f"DEPLOYED: fleet control -> {destination}")
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
    source = component_path(component, host) / component["build_output"]
    if not source.is_file():
        print(f"fleetctl: build output is missing: {source}", file=sys.stderr)
        return 2
    install_dir = component_install_dir(component_name, component, host)
    install_dir.mkdir(parents=True, exist_ok=True)
    if component_name == "studio":
        current = install_dir / "current"
        if current.exists() or current.is_symlink():
            print(
                "fleetctl: Studio source install is bootstrap-only once runtime/studio/current exists",
                file=sys.stderr,
            )
            return 2
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



SELF_UPDATE_SCHEMA_VERSION = 1
SELF_UPDATE_HEALTH_TIMEOUT_SECONDS = 20.0
SELF_UPDATE_PARENT_EXIT_TIMEOUT_SECONDS = 15.0
SELF_UPDATE_POLL_SECONDS = 0.2
SELF_UPDATE_MAX_FILES = 8192
SELF_UPDATE_MAX_BYTES = 1024 * 1024 * 1024


def safe_self_update_transaction_id(value: str) -> bool:
    return (
        value.startswith("txn-studio-")
        and len(value) <= 128
        and all(char.isalnum() or char in "-_" for char in value)
    )


def self_update_transaction_path(host: dict[str, Any], transaction_id: str) -> Path:
    if not safe_self_update_transaction_id(transaction_id):
        raise RuntimeError("invalid Studio self-update transaction id")
    runtime_root = Path(host["runtime_root"]).resolve()
    return runtime_root / "studio" / "data" / "self-update" / f"{transaction_id}.json"


def read_regular_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"self-update metadata is not a regular file: {path}")
    return json.loads(path.read_text())


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(temp_fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
        fsync_dir(path.parent)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def self_update_tree_fingerprint(root: Path) -> str:
    resolved_root = root.resolve()
    if root.is_symlink() or not resolved_root.is_dir():
        raise RuntimeError("Studio self-update release candidate must be a regular directory")

    files: list[tuple[str, str]] = []
    total = 0
    for current, dirs, names in os.walk(resolved_root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirs):
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError("Studio self-update candidate contains a symlink")
        for name in names:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("Studio self-update candidate contains an unsafe entry")
            relative = path.relative_to(resolved_root).as_posix()
            data = path.read_bytes()
            total += len(data)
            if len(files) >= SELF_UPDATE_MAX_FILES or total > SELF_UPDATE_MAX_BYTES:
                raise RuntimeError("Studio self-update candidate exceeds safety limits")
            files.append((relative, hashlib.sha256(data).hexdigest()))

    files.sort()
    digest = hashlib.sha256()
    for relative, file_digest in files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def validate_self_update_release(release: Path, version: str, fingerprint: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError("invalid Studio target version")
    binary = release / "mcp-studio"
    web_index = release / "web" / "dist" / "index.html"
    if binary.is_symlink() or not binary.is_file():
        raise RuntimeError("Studio target binary is unavailable")
    if web_index.is_symlink() or not web_index.is_file():
        raise RuntimeError("Studio target web/dist/index.html is unavailable")
    actual_version = binary_version(binary)
    if actual_version != version:
        raise RuntimeError(
            f"Studio target binary version mismatch: expected {version}, got {actual_version}"
        )
    if self_update_tree_fingerprint(release) != fingerprint:
        raise RuntimeError("Studio target release fingerprint mismatch")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def wait_for_pid_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(SELF_UPDATE_POLL_SECONDS)
    return not pid_alive(pid)


def safe_release_target(studio_root: Path, target: str) -> Path:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", target):
        raise RuntimeError("invalid Studio release directory")
    releases = (studio_root / "releases").resolve()
    releases.mkdir(parents=True, exist_ok=True)
    path = releases / target
    if path.resolve(strict=False).parent != releases:
        raise RuntimeError("Studio release path escaped releases root")
    return path


def candidate_target(studio_root: Path, transaction_id: str, candidate_name: str) -> Path:
    expected = f".candidate-{transaction_id}"
    if candidate_name != expected:
        raise RuntimeError("Studio candidate directory identity mismatch")
    releases = (studio_root / "releases").resolve()
    releases.mkdir(parents=True, exist_ok=True)
    path = releases / candidate_name
    if path.resolve(strict=False).parent != releases:
        raise RuntimeError("Studio candidate path escaped releases root")
    return path


def current_release_target(studio_root: Path) -> str | None:
    current = studio_root / "current"
    if not current.exists() and not current.is_symlink():
        return None
    if not current.is_symlink():
        raise RuntimeError("Studio current activation pointer is not a symlink")
    target = os.readlink(current)
    path = Path(target)
    if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != "releases":
        raise RuntimeError("Studio current activation pointer is unsafe")
    release_name = path.parts[1]
    safe_release_target(studio_root, release_name)
    resolved = (studio_root / path).resolve()
    releases = (studio_root / "releases").resolve()
    if resolved.parent != releases or not resolved.is_dir():
        raise RuntimeError("Studio current release target is unavailable")
    return release_name


def atomic_set_current(studio_root: Path, release_name: str, transaction_id: str) -> None:
    safe_release_target(studio_root, release_name)
    current = studio_root / "current"
    if current.exists() and not current.is_symlink():
        raise RuntimeError("Studio current activation pointer is not a symlink")
    temp = studio_root / f".current-{transaction_id}.tmp"
    if temp.exists() or temp.is_symlink():
        temp.unlink()
    os.symlink(f"releases/{release_name}", temp)
    os.replace(temp, current)
    fsync_dir(studio_root)


def clear_current(studio_root: Path) -> None:
    current = studio_root / "current"
    if current.is_symlink():
        current.unlink()
        fsync_dir(studio_root)
    elif current.exists():
        raise RuntimeError("Studio current activation pointer is not a symlink")


def studio_listen_url(studio_root: Path) -> str:
    config_path = studio_root / "studio.toml"
    config = load_toml(config_path)
    listen = str(config.get("server", {}).get("listen_addr", "127.0.0.1:18100"))
    if listen.count(":") != 1:
        raise RuntimeError("Studio self-update health endpoint requires IPv4 loopback listen_addr")
    host, port_text = listen.rsplit(":", 1)
    if host not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Studio self-update health endpoint must be loopback")
    port = int(port_text)
    if port <= 0 or port > 65535:
        raise RuntimeError("Studio self-update health port is invalid")
    return f"http://{host}:{port}/health"


def studio_health(studio_root: Path, expected_version: str) -> bool:
    try:
        with urllib.request.urlopen(studio_listen_url(studio_root), timeout=1.0) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read(64 * 1024))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        payload.get("status") == "ok"
        and payload.get("service") == "mcp-studio"
        and payload.get("version") == expected_version
    )


def wait_for_studio_health(studio_root: Path, expected_version: str) -> bool:
    deadline = time.monotonic() + SELF_UPDATE_HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if studio_health(studio_root, expected_version):
            return True
        time.sleep(SELF_UPDATE_POLL_SECONDS)
    return studio_health(studio_root, expected_version)


def minimal_studio_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
    }
    for key in ("HOME", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def spawn_studio(studio_root: Path, binary: Path, cwd: Path) -> subprocess.Popen[bytes]:
    config_path = studio_root / "studio.toml"
    if binary.is_symlink() or not binary.is_file():
        raise RuntimeError("Studio launch binary is unavailable")
    return subprocess.Popen(
        [str(binary), "--config", str(config_path)],
        cwd=cwd,
        env=minimal_studio_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop_spawned_process(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def update_self_update_metadata(
    path: Path,
    metadata: dict[str, Any],
    phase: str,
    *,
    error: str | None = None,
    rollback_succeeded: bool | None = None,
) -> None:
    metadata["phase"] = phase
    metadata["error"] = error
    metadata["rollback_succeeded"] = rollback_succeeded
    metadata["updated_at_ms"] = int(time.time() * 1000)
    write_json_atomic(path, metadata)


def validate_self_update_metadata(
    metadata: dict[str, Any],
    transaction_id: str,
    parent_pid: int,
) -> None:
    if metadata.get("schema_version") != SELF_UPDATE_SCHEMA_VERSION:
        raise RuntimeError("unsupported Studio self-update metadata schema")
    if metadata.get("transaction_id") != transaction_id:
        raise RuntimeError("Studio self-update transaction identity mismatch")
    if metadata.get("component") != "studio":
        raise RuntimeError("Studio self-update metadata component mismatch")
    if metadata.get("parent_pid") != parent_pid:
        raise RuntimeError("Studio self-update parent PID mismatch")
    if metadata.get("phase") not in {
        "activation_pending",
        "external_activating",
        "external_activated",
    }:
        raise RuntimeError("Studio self-update transaction is not activation-pending")


def rollback_studio_release(
    studio_root: Path,
    metadata_path: Path,
    metadata: dict[str, Any],
    transaction_id: str,
    previous_layout: str,
    previous_release: str | None,
    legacy_binary: Path,
    source_version: str,
    reason: str,
) -> int:
    update_self_update_metadata(
        metadata_path,
        metadata,
        "rolling_back",
        error=reason,
    )
    if previous_layout == "versioned":
        if previous_release is None:
            update_self_update_metadata(
                metadata_path,
                metadata,
                "rollback_failed",
                error="rollback_previous_release_missing",
                rollback_succeeded=False,
            )
            return 4
        atomic_set_current(studio_root, previous_release, transaction_id)
        rollback_binary = studio_root / "releases" / previous_release / "mcp-studio"
        rollback_cwd = rollback_binary.parent
    else:
        clear_current(studio_root)
        rollback_binary = legacy_binary
        rollback_cwd = studio_root

    rollback_proc = spawn_studio(studio_root, rollback_binary, rollback_cwd)
    if wait_for_studio_health(studio_root, source_version):
        update_self_update_metadata(
            metadata_path,
            metadata,
            "rolled_back",
            error=reason,
            rollback_succeeded=True,
        )
        return 3

    stop_spawned_process(rollback_proc)
    update_self_update_metadata(
        metadata_path,
        metadata,
        "rollback_failed",
        error="rollback_health_failed",
        rollback_succeeded=False,
    )
    return 4


def studio_activate(host_name: str, transaction_id: str, parent_pid: int) -> int:
    host = load_toml(FLEET_DIR / "hosts" / f"{host_name}.toml")
    runtime_root = Path(host["runtime_root"]).resolve()
    studio_root = runtime_root / "studio"
    metadata_path = self_update_transaction_path(host, transaction_id)
    metadata = read_regular_json(metadata_path)
    validate_self_update_metadata(metadata, transaction_id, parent_pid)

    source_version = str(metadata["source_version"])
    target_version = str(metadata["target_version"])
    candidate_name = str(metadata["candidate_dir"])
    target_release = str(metadata["target_release"])
    fingerprint = str(metadata["candidate_fingerprint"])

    releases = studio_root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    candidate = candidate_target(studio_root, transaction_id, candidate_name)
    target = safe_release_target(studio_root, target_release)

    # Resume-safe fast path after a launcher interruption.
    if current_release_target(studio_root) == target_release and target.is_dir():
        validate_self_update_release(target, target_version, fingerprint)
        if studio_health(studio_root, target_version):
            update_self_update_metadata(metadata_path, metadata, "completed")
            return 0

    if metadata["phase"] == "activation_pending":
        if not wait_for_pid_exit(parent_pid, SELF_UPDATE_PARENT_EXIT_TIMEOUT_SECONDS):
            update_self_update_metadata(
                metadata_path,
                metadata,
                "activation_failed",
                error="parent_exit_timeout",
            )
            return 2

    update_self_update_metadata(metadata_path, metadata, "external_activating")

    legacy_binary = studio_root / "mcp-studio"
    previous_layout = metadata.get("previous_layout")
    previous_release = metadata.get("previous_release")
    if previous_layout is None:
        previous_release = current_release_target(studio_root)
        if previous_release is not None:
            previous_layout = "versioned"
        elif legacy_binary.is_file() and not legacy_binary.is_symlink():
            previous_layout = "legacy_flat"
        else:
            update_self_update_metadata(
                metadata_path,
                metadata,
                "activation_failed",
                error="previous_release_unavailable",
            )
            return 2
        metadata["previous_layout"] = previous_layout
        metadata["previous_release"] = previous_release
        write_json_atomic(metadata_path, metadata)

    if previous_layout == "versioned":
        if not isinstance(previous_release, str):
            update_self_update_metadata(
                metadata_path,
                metadata,
                "activation_failed",
                error="previous_release_unavailable",
            )
            return 2
        previous_binary = studio_root / "releases" / previous_release / "mcp-studio"
    elif previous_layout == "legacy_flat":
        previous_binary = legacy_binary
    else:
        update_self_update_metadata(
            metadata_path,
            metadata,
            "activation_failed",
            error="previous_layout_invalid",
        )
        return 2

    if binary_version(previous_binary) != source_version:
        update_self_update_metadata(
            metadata_path,
            metadata,
            "activation_failed",
            error="source_version_mismatch",
        )
        return 2

    switched = False
    target_proc: subprocess.Popen[bytes] | None = None
    try:
        if target.exists():
            validate_self_update_release(target, target_version, fingerprint)
            if candidate.exists():
                if self_update_tree_fingerprint(candidate) != fingerprint:
                    raise RuntimeError("Studio candidate fingerprint changed")
                shutil.rmtree(candidate)
        else:
            validate_self_update_release(candidate, target_version, fingerprint)
            os.replace(candidate, target)
            fsync_dir(releases)

        atomic_set_current(studio_root, target_release, transaction_id)
        switched = True
        update_self_update_metadata(metadata_path, metadata, "external_activated")

        target_proc = spawn_studio(studio_root, target / "mcp-studio", target)
        if wait_for_studio_health(studio_root, target_version):
            update_self_update_metadata(metadata_path, metadata, "completed")
            return 0

        stop_spawned_process(target_proc)
        return rollback_studio_release(
            studio_root,
            metadata_path,
            metadata,
            transaction_id,
            previous_layout,
            previous_release,
            legacy_binary,
            source_version,
            "target_health_failed",
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        stop_spawned_process(target_proc)
        print(f"fleetctl: Studio activation failed: {exc}", file=sys.stderr)
        if switched:
            try:
                return rollback_studio_release(
                    studio_root,
                    metadata_path,
                    metadata,
                    transaction_id,
                    previous_layout,
                    previous_release,
                    legacy_binary,
                    source_version,
                    "launcher_activation_failed",
                )
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as rollback_exc:
                update_self_update_metadata(
                    metadata_path,
                    metadata,
                    "rollback_failed",
                    error="rollback_exception",
                    rollback_succeeded=False,
                )
                print(
                    f"fleetctl: Studio rollback failed after launcher exception: {rollback_exc}",
                    file=sys.stderr,
                )
                return 4
        update_self_update_metadata(
            metadata_path,
            metadata,
            "activation_failed",
            error="launcher_activation_failed",
        )
        return 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MCP fleet foundation tool")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "doctor", "snapshot"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--host", required=True)
    doctor_parser = sub.choices["doctor"]
    doctor_parser.add_argument("--require-remotes", action="store_true")
    studio_activate_parser = sub.add_parser("studio-activate")
    studio_activate_parser.add_argument("--host", required=True)
    studio_activate_parser.add_argument("--transaction", required=True)
    studio_activate_parser.add_argument("--parent-pid", required=True, type=int)
    render_plan_parser = sub.add_parser("render-plan")
    render_plan_parser.add_argument("--host", required=True)
    render_plan_parser.add_argument("--json", action="store_true")
    render = sub.add_parser("render-gateway")
    render.add_argument("--host", required=True)
    render.add_argument("--check", action="store_true")
    render_studio_parser = sub.add_parser("render-studio")
    render_studio_parser.add_argument("--host", required=True)
    render_studio_parser.add_argument("--check", action="store_true")
    render_tunnel_parser = sub.add_parser("render-tunnel")
    render_tunnel_parser.add_argument("--host", required=True)
    render_tunnel_parser.add_argument("--check", action="store_true")
    deploy_parser = sub.add_parser("deploy-control")
    deploy_parser.add_argument("--host", required=True)
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
        if args.command == "studio-activate":
            return studio_activate(args.host, args.transaction, args.parent_pid)
        if args.command == "render-plan":
            return render_plan(args.host, args.json)
        if args.command == "render-gateway":
            return render_gateway(args.host, args.check)
        if args.command == "render-studio":
            return render_studio(args.host, args.check)
        if args.command == "render-tunnel":
            return render_tunnel_config(args.host, args.check)
        if args.command == "deploy-control":
            return deploy_control(args.host)
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
