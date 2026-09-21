# Mirin SonarQube runtime

SonarQube uses the single shared PostgreSQL service already running on Mirin. It does not start another PostgreSQL container.

## Database model

```text
shared-postgres
├── existing project databases / roles
└── sonar database
    └── sonar login role
```

The SonarQube container joins the existing external Docker network `ophiuchus-network` and connects to `shared-postgres:5432`. PostgreSQL does not need another published port.

## Bootstrap

Provision the dedicated Sonar database/role and host-local runtime secrets:

```bash
python3 bootstrap_shared_db.py
```

The bootstrap requires:
- container `shared-postgres` to be running;
- PostgreSQL superuser `postgres` accessible inside that container;
- UTF-8 server encoding;
- no pre-existing `sonar` database or role.

It writes only host-local secret files under `~/.config/sonarqube/` with mode 0600 and does not print passwords.

## Harden the fresh instance

After the first successful `UP` state, replace the default `admin/admin` credential using the generated host-local password:

```bash
python3 harden_admin.py
```

The helper validates the replacement credential and never prints it.

## Start

Keep SonarQube loopback-only until WireGuard between Aira and Mirin is verified:

```bash
export SONARQUBE_RUNTIME_ENV="$HOME/.config/sonarqube/runtime.env"
export SONARQUBE_BIND_ADDRESS=127.0.0.1
docker compose -f compose.yaml up -d
```

After WireGuard is ready, set `SONARQUBE_BIND_ADDRESS` to Mirin's WireGuard address only. Do not bind SonarQube to `0.0.0.0` and do not publish raw port 9000 to the Internet.

## Ownership

The shared PostgreSQL container, its volume, backups, and lifecycle are platform infrastructure. SonarQube owns only:
- database `sonar`;
- role `sonar`;
- Sonar application volumes `sonarqube_data`, `sonarqube_extensions`, and `sonarqube_logs`.

Dropping or recreating the shared PostgreSQL service is never part of SonarQube lifecycle operations.