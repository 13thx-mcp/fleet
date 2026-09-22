#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request(url: str, username: str, password: str, data: dict[str, str] | None = None) -> tuple[int, bytes]:
    auth = base64.b64encode(f"{username}:{password}".encode()).decode()
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    parser = argparse.ArgumentParser(description="Replace fresh SonarQube admin/admin password")
    parser.add_argument("--server-url", default="http://127.0.0.1:9000")
    parser.add_argument("--password-file", default="~/.config/sonarqube/admin-password")
    args = parser.parse_args()

    password = Path(args.password_file).expanduser().read_text(encoding="utf-8").strip()
    if not password:
        raise SystemExit("admin password file is empty")

    base = args.server_url.rstrip("/")
    status, response_body = request(
        f"{base}/api/users/change_password",
        "admin",
        "admin",
        {
            "login": "admin",
            "previousPassword": "admin",
            "password": password,
        },
    )
    if status not in (200, 204):
        detail = response_body.decode("utf-8", errors="replace")[:1000]
        raise SystemExit(f"default admin password change failed with HTTP {status}: {detail}")

    # Prove the new credential works without printing it.
    auth = base64.b64encode(f"admin:{password}".encode()).decode()
    req = urllib.request.Request(
        f"{base}/api/authentication/validate",
        headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        body = response.read().decode("utf-8")
    if '"valid":true' not in body.replace(" ", ""):
        raise SystemExit("new admin credential validation failed")

    print("default SonarQube admin password replaced and validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
