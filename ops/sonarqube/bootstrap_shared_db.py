#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import subprocess
from pathlib import Path


ROLE = "sonar"
DATABASE = "sonar"


def run(cmd: list[str], *, stdin: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"command failed: {cmd[0]}")
    return completed


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def psql_scalar(sql: str) -> str:
    return run([
        "docker", "exec", "shared-postgres",
        "psql", "-U", "postgres", "-Atqc", sql,
    ]).stdout.strip()


def psql_exec(sql: str) -> None:
    run([
        "docker", "exec", "-i", "shared-postgres",
        "psql", "-U", "postgres", "-v", "ON_ERROR_STOP=1",
    ], stdin=sql)


def write_atomic_secret(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def ensure_admin_password(path: Path) -> None:
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        os.chmod(path, 0o600)
        return
    password = secrets.token_urlsafe(36) + "Aa1!"
    write_atomic_secret(path, password + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision SonarQube database in Mirin shared PostgreSQL")
    parser.add_argument("--runtime-env", default="~/.config/sonarqube/runtime.env")
    parser.add_argument("--admin-password-file", default="~/.config/sonarqube/admin-password")
    args = parser.parse_args()

    if psql_scalar("SHOW server_encoding;") != "UTF8":
        raise SystemExit("shared PostgreSQL must use UTF8 server encoding")

    if psql_scalar(f"SELECT 1 FROM pg_roles WHERE rolname={sql_literal(ROLE)};"):
        raise SystemExit(
            "role sonar already exists; refusing to rotate its password implicitly. "
            "Use the existing host-local Sonar credential or perform an explicit credential rotation."
        )
    if psql_scalar(f"SELECT 1 FROM pg_database WHERE datname={sql_literal(DATABASE)};"):
        raise SystemExit("database sonar already exists without the managed sonar role; inspect before changing it")

    db_password = secrets.token_urlsafe(36)
    jwt_secret = base64.b64encode(os.urandom(32)).decode("ascii")

    psql_exec(
        f"CREATE ROLE {ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
        f"PASSWORD {sql_literal(db_password)};\n"
    )
    try:
        psql_exec(f"CREATE DATABASE {DATABASE} OWNER {ROLE} ENCODING 'UTF8';\n")
    except Exception:
        psql_exec(f"DROP ROLE IF EXISTS {ROLE};\n")
        raise

    runtime_env = Path(args.runtime_env).expanduser()
    write_atomic_secret(
        runtime_env,
        "\n".join([
            "SONAR_JDBC_URL=jdbc:postgresql://shared-postgres:5432/sonar",
            "SONAR_JDBC_USERNAME=sonar",
            f"SONAR_JDBC_PASSWORD={db_password}",
            f"SONAR_AUTH_JWTBASE64HS256SECRET={jwt_secret}",
            "",
        ]),
    )
    ensure_admin_password(Path(args.admin_password_file).expanduser())

    print("provisioned database: sonar")
    print("provisioned role: sonar")
    print(f"updated runtime env: {runtime_env}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
