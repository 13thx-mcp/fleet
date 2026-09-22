#!/usr/bin/env python3
"""Reject host-local metadata and secrets from Fleet source files."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

USER_HOME_PATH_RE = re.compile("/" + "Users" + r"/[^/\s]+/")
HOST_LOCAL_PATH_RES = (
    USER_HOME_PATH_RE,
    re.compile("/" + "home" + r"/[^/\s]+/"),
    re.compile("/" + "private" + "/" + "var" + "/" + "folders" + "/"),
    re.compile("/" + "var" + "/" + "folders" + "/"),
)
TUNNEL_ID_RE = re.compile(r"tunnel_[0-9a-f]{32}")
OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
BEARER_RE = re.compile(
    r"(?i)authorization\s*[:=]\s*[\"']?bearer\s+[A-Za-z0-9._~+/-]{16,}"
)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9._-]{16,}"
)


def source_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return [root / line for line in result.stdout.splitlines()]


def is_tunnel_example(path: Path) -> bool:
    return path.parts[0:1] == ("tests",) or path.name.endswith(".example.toml")


def violations_for(path: Path, root: Path) -> list[str]:
    relative = path.relative_to(root)
    relative_text = relative.as_posix()
    if "/credentials/" in f"/{relative_text}":
        return ["credential file tracked in source"]
    if relative.name.startswith(".env") and not relative.name.endswith(".example"):
        return ["environment file tracked in source"]

    text = path.read_text(encoding="utf-8", errors="replace")
    findings: list[str] = []
    if any(pattern.search(text) for pattern in HOST_LOCAL_PATH_RES):
        findings.append("machine-specific host path")
    if TUNNEL_ID_RE.search(text) and not is_tunnel_example(relative):
        findings.append("tunnel identity outside test/example")
    if OPENAI_KEY_RE.search(text):
        findings.append("probable OpenAI API key")
    if BEARER_RE.search(text):
        findings.append("probable bearer credential")
    if SECRET_ASSIGNMENT_RE.search(text):
        findings.append("probable credential assignment")
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="check Fleet source hygiene")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    root = args.root.resolve()
    violations: list[tuple[Path, str]] = []
    for path in source_files(root):
        if path.is_file():
            violations.extend((path.relative_to(root), finding) for finding in violations_for(path, root))
    for path, finding in violations:
        print(f"{path}: {finding}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
