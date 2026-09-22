#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PROJECT_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
PASS = 0
FAIL = 2
ERROR = 3
STALE = 4


class GateError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        raise GateError(
            f"command failed ({completed.returncode}): {cmd[0]}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed


def git(repo: Path, *args: str) -> str:
    return run(["git", "-C", str(repo), *args], cwd=repo).stdout.strip()


def canonical_repo(repo: Path) -> Path:
    resolved = repo.expanduser().resolve()
    root = Path(git(resolved, "rev-parse", "--show-toplevel")).resolve()
    if root != resolved:
        raise GateError(f"repo must be exact Git root: expected {root}, got {resolved}")
    return root


def validate_project_key(project_key: str) -> None:
    if not PROJECT_KEY_RE.fullmatch(project_key):
        raise GateError("invalid Sonar project key")


def repo_state(repo: Path, expected_head: str) -> dict[str, str]:
    branch = git(repo, "branch", "--show-current")
    if branch != "main":
        raise GateError(f"Sonar release gate requires branch main, got {branch or '<detached>'}")
    status = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise GateError("Sonar release gate requires a clean worktree/index")
    head = git(repo, "rev-parse", "HEAD")
    expected = git(repo, "rev-parse", expected_head)
    if head != expected:
        raise GateError(f"HEAD mismatch: expected {expected}, got {head}")
    shallow = git(repo, "rev-parse", "--is-shallow-repository")
    if shallow == "true":
        raise GateError("Sonar release gate refuses shallow repositories")
    return {"branch": branch, "head": head}


def load_config(path: Path) -> dict[str, Any]:
    config_path = path.expanduser()
    if not config_path.is_file():
        raise GateError(f"Sonar gate config not found: {config_path}")
    with config_path.open("rb") as handle:
        config = tomllib.load(handle)
    for required in ("server_url", "scanner", "token_dir", "evidence_root"):
        if not config.get(required):
            raise GateError(f"Sonar gate config missing {required}")
    return config


def token_for(config: dict[str, Any], project_key: str) -> str:
    validate_project_key(project_key)
    path = Path(str(config["token_dir"])).expanduser() / f"{project_key}.token"
    if not path.is_file():
        raise GateError(f"project analysis token file not found: {path}")
    token = path.read_text().strip()
    if not token:
        raise GateError(f"project analysis token is empty: {path}")
    return token


def evidence_path(config: dict[str, Any], project_key: str, head: str) -> Path:
    validate_project_key(project_key)
    return Path(str(config["evidence_root"])).expanduser() / project_key / f"{head}.json"


def lock_path(config: dict[str, Any], repo: Path) -> Path:
    digest = hashlib.sha256(str(repo).encode()).hexdigest()[:24]
    root = Path(str(config["evidence_root"])).expanduser().parent / "locks"
    return root / f"{digest}.lock"


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def auth_request(url: str, token: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise GateError(f"SonarQube request failed: {url}: {exc}") from exc


def server_health(server_url: str) -> dict[str, Any]:
    base = server_url.rstrip("/")
    payload = auth_request(f"{base}/api/system/status")
    if payload.get("status") != "UP":
        raise GateError(f"SonarQube is not UP: {payload.get('status')!r}")
    return payload


def parse_report_task(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise GateError(f"scanner report-task missing: {path}")
    result: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        result[key.strip()] = value.strip()
    for required in ("ceTaskId", "ceTaskUrl", "serverUrl"):
        if not result.get(required):
            raise GateError(f"scanner report-task missing {required}")
    return result


def wait_for_ce(ce_task_url: str, token: str, timeout_seconds: int, poll_seconds: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        payload = auth_request(ce_task_url, token)
        task = payload.get("task") or {}
        status = task.get("status")
        if status == "SUCCESS":
            if not task.get("analysisId"):
                raise GateError("successful Compute Engine task has no analysisId")
            return task
        if status in {"FAILED", "CANCELED"}:
            raise GateError(f"Compute Engine task ended as {status}")
        if time.monotonic() >= deadline:
            raise GateError(f"Compute Engine task did not finish within {timeout_seconds}s")
        time.sleep(poll_seconds)


def quality_gate(server_url: str, analysis_id: str, token: str) -> dict[str, Any]:
    query = urllib.parse.urlencode({"analysisId": analysis_id})
    payload = auth_request(f"{server_url.rstrip('/')}/api/qualitygates/project_status?{query}", token)
    project_status = payload.get("projectStatus")
    if not isinstance(project_status, dict) or not project_status.get("status"):
        raise GateError("quality gate response missing projectStatus.status")
    return project_status


def config_fingerprint(repo: Path) -> str | None:
    path = repo / "sonar-project.properties"
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_evidence(
    *,
    status: str,
    repo: Path,
    project_key: str,
    head: str,
    started_at: str,
    report: dict[str, str] | None = None,
    ce_task: dict[str, Any] | None = None,
    gate: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "gate": "sonarqube-main",
        "status": status,
        "repo": str(repo),
        "project_key": project_key,
        "commit": head,
        "branch": "main",
        "started_at": started_at,
        "completed_at": utc_now(),
        "config_sha256": config_fingerprint(repo),
        "ce_task_id": (report or {}).get("ceTaskId"),
        "ce_task_url": (report or {}).get("ceTaskUrl"),
        "analysis_id": (ce_task or {}).get("analysisId"),
        "quality_gate": gate,
        "error": error,
    }


def scan(args: argparse.Namespace) -> int:
    repo = canonical_repo(Path(args.repo))
    validate_project_key(args.project_key)
    config = load_config(Path(args.config))
    state = repo_state(repo, args.expected_head)
    head = state["head"]
    server_url = str(config["server_url"]).rstrip("/")
    token = token_for(config, args.project_key)
    scanner = str(Path(str(config["scanner"])).expanduser())
    if not Path(scanner).is_file():
        raise GateError(f"sonar-scanner not found: {scanner}")
    server_health(server_url)

    lock = lock_path(config, repo)
    lock.parent.mkdir(parents=True, exist_ok=True)
    started_at = utc_now()
    report: dict[str, str] | None = None
    ce_task: dict[str, Any] | None = None
    gate: dict[str, Any] | None = None
    evidence_file = evidence_path(config, args.project_key, head)

    with lock.open("a+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise GateError(f"Sonar gate already running for repository: {repo}") from exc

        try:
            env = os.environ.copy()
            env["SONAR_HOST_URL"] = server_url
            env["SONAR_TOKEN"] = token
            completed = run(
                [
                    scanner,
                    f"-Dsonar.projectKey={args.project_key}",
                    f"-Dsonar.scm.revision={head}",
                ],
                cwd=repo,
                env=env,
                check=False,
            )
            if completed.returncode != 0:
                evidence = make_evidence(
                    status="ERROR",
                    repo=repo,
                    project_key=args.project_key,
                    head=head,
                    started_at=started_at,
                    error=f"sonar-scanner exited {completed.returncode}",
                )
                write_json_atomic(evidence_file, evidence)
                print(json.dumps(evidence, sort_keys=True))
                return ERROR

            report = parse_report_task(repo / ".scannerwork" / "report-task.txt")
            if report["serverUrl"].rstrip("/") != server_url:
                raise GateError(
                    f"scanner reported unexpected server: {report['serverUrl']} != {server_url}"
                )
            ce_task = wait_for_ce(
                report["ceTaskUrl"],
                token,
                int(config.get("ce_timeout_seconds", 600)),
                float(config.get("poll_seconds", 2.0)),
            )
            gate = quality_gate(server_url, str(ce_task["analysisId"]), token)

            try:
                final_state = repo_state(repo, head)
            except GateError as exc:
                evidence = make_evidence(
                    status="STALE",
                    repo=repo,
                    project_key=args.project_key,
                    head=head,
                    started_at=started_at,
                    report=report,
                    ce_task=ce_task,
                    gate=gate,
                    error=str(exc),
                )
                write_json_atomic(evidence_file, evidence)
                print(json.dumps(evidence, sort_keys=True))
                return STALE
            if final_state["head"] != head:
                raise GateError("repository HEAD changed during Sonar gate")

            result = "PASS" if gate.get("status") == "OK" else "FAIL"
            evidence = make_evidence(
                status=result,
                repo=repo,
                project_key=args.project_key,
                head=head,
                started_at=started_at,
                report=report,
                ce_task=ce_task,
                gate=gate,
            )
            write_json_atomic(evidence_file, evidence)
            print(json.dumps(evidence, sort_keys=True))
            return PASS if result == "PASS" else FAIL
        except GateError as exc:
            evidence = make_evidence(
                status="ERROR",
                repo=repo,
                project_key=args.project_key,
                head=head,
                started_at=started_at,
                report=report,
                ce_task=ce_task,
                gate=gate,
                error=str(exc),
            )
            write_json_atomic(evidence_file, evidence)
            print(json.dumps(evidence, sort_keys=True))
            return ERROR


def verify(args: argparse.Namespace) -> int:
    repo = canonical_repo(Path(args.repo))
    validate_project_key(args.project_key)
    config = load_config(Path(args.config))
    state = repo_state(repo, args.expected_head)
    path = evidence_path(config, args.project_key, state["head"])
    if not path.is_file():
        raise GateError(f"release evidence not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "schema_version": SCHEMA_VERSION,
        "gate": "sonarqube-main",
        "status": "PASS",
        "repo": str(repo),
        "project_key": args.project_key,
        "commit": state["head"],
        "branch": "main",
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise GateError(f"release evidence mismatch for {key}: {payload.get(key)!r} != {value!r}")
    if not payload.get("analysis_id"):
        raise GateError("release evidence has no analysis_id")
    print(json.dumps(payload, sort_keys=True))
    return PASS


def preflight(args: argparse.Namespace) -> int:
    repo = canonical_repo(Path(args.repo))
    validate_project_key(args.project_key)
    config = load_config(Path(args.config))
    state = repo_state(repo, args.expected_head)
    health = server_health(str(config["server_url"]))
    result = {
        "status": "READY",
        "repo": str(repo),
        "project_key": args.project_key,
        "commit": state["head"],
        "server_status": health.get("status"),
        "scanner": str(Path(str(config["scanner"])).expanduser()),
    }
    print(json.dumps(result, sort_keys=True))
    return PASS


def parser() -> argparse.ArgumentParser:
    default_config = "~/.config/sonarqube/gate.toml"
    root = argparse.ArgumentParser(description="Exact-SHA SonarQube main-branch release gate")
    sub = root.add_subparsers(dest="command", required=True)
    for name in ("preflight", "scan", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--repo", required=True)
        command.add_argument("--project-key", required=True)
        command.add_argument("--expected-head", required=True)
        command.add_argument("--config", default=default_config)
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "preflight":
            return preflight(args)
        if args.command == "scan":
            return scan(args)
        if args.command == "verify":
            return verify(args)
        raise GateError(f"unsupported command: {args.command}")
    except GateError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return ERROR


if __name__ == "__main__":
    raise SystemExit(main())
