# SonarQube main-branch release gate

## Decision

Use SonarQube Community Build only as an integrated `main` verification gate.

The release lifecycle is:

```text
work branch
  -> project-native tests/review
  -> version + changelog + release-prep commit
  -> no-ff merge to main
  -> exact-SHA Sonar analysis
  -> quality gate PASS
  -> release tag / finalizer
```

A Sonar FAIL never permits a direct edit on `main`. Triage first, then create a remediation branch and run the normal lifecycle again.

## Network model

SonarQube does not require a public IP or public DNS name for this workflow. The only mandatory path is Aira -> Mirin TCP access to the SonarQube web/API endpoint.

Preferred order:

1. WireGuard private overlay with Mirin as host/server and Aira as peer/client;
2. private routed LAN/VPN;
3. public HTTPS reverse proxy only when a private route is impossible.

Do not publish raw port 9000 to the Internet.

Current discovery on 2026-09-21 found:
- Aira and Mirin each have a 10.8.0.x tunnel interface, but Aira cannot reach Mirin over that network;
- Tailscale is installed on both hosts, but both clients are stopped and are enrolled in different tailnets;
- therefore end-to-end Aira -> Mirin connectivity is not yet available.

Until WireGuard is established, Mirin binds SonarQube to loopback only. Afterward, bind SonarQube specifically to Mirin's WireGuard address rather than `0.0.0.0`.

## Server

- Image: `sonarqube:26.9.0.129388-community`
- Database: `postgres:17-alpine`
- Persistent named volumes for Sonar data/extensions/logs and PostgreSQL data.
- Secrets live in `~/.config/sonarqube/runtime.env` on Mirin and are never committed.
- Default bind is `127.0.0.1:9000`.
- Authentication remains forced.
- Default admin password must be changed during bootstrap before remote access is enabled.

## Aira scanner gate

`scripts/sonar_gate.py` is the machine-verifiable release gate.

Inputs:
- repository path;
- Sonar project key;
- expected integrated main SHA.

Preconditions:
- repository root is exact;
- branch is `main`;
- worktree/index/untracked state is clean;
- checkout is not shallow;
- HEAD equals expected SHA;
- SonarQube reports `UP`.

Execution:
1. acquire a per-repository lock;
2. run the configured native `sonar-scanner` with token in environment, never argv;
3. parse `.scannerwork/report-task.txt`;
4. wait for the exact Compute Engine task;
5. obtain its `analysisId`;
6. query the Quality Gate for that exact analysis;
7. re-check HEAD and cleanliness;
8. persist non-secret evidence outside the repository.

Evidence status:
- `PASS`: exact analysis completed and Quality Gate is OK;
- `FAIL`: analysis completed but Quality Gate is not OK;
- `ERROR`: scanner/network/processing failure;
- `STALE`: repository no longer matches the analyzed SHA.

Only `PASS` evidence for the current release HEAD is eligible for finalization.

## Release-tag enforcement

The Git MCP gains an optional generic release-evidence policy. When a repository is enrolled, `git_release_tag` must find PASS evidence matching current HEAD before creating a tag.

The policy is opt-in per repository so projects can be enrolled after a clean baseline exists. It must not silently treat missing evidence as PASS.

## Official SonarQube MCP

Use the official SonarQube MCP image in read-only mode. The Aira launcher is pinned to image digest sha256:21bb7bf785a8c9cbe19553f6957d83ef45138926bbb15ca9e1fc4895e5026b6f (release 1.27.0.4335), so the Gateway child does not depend on a mutable tag.

This MCP is inspection/triage only. It does not create the authoritative release evidence and cannot override the scanner gate.

Aira Fleet owns launchers/sonarqube-mcp and deploys it into the trusted flat bin/ root. Gateway config references that launcher rather than /usr/local/bin/docker directly, so Studio can independently probe the child without weakening its trusted-executable boundary. The launcher fixes Docker argv, read-only mode, and the image digest; the only runtime argument is the absolute host-local env-file path.

The Aira profile keeps this child disabled until all of the following are verified:
- Aira -> Mirin private connectivity;
- Docker Desktop running on Aira;
- ~/.config/sonarqube/mcp.env containing a low-privilege SonarQube USER token and the private SonarQube URL.

## Jev boundary

On Sonar FAIL, Jev may be used to route the classification/triage prompt to an appropriate model. Jev output is advisory and probabilistic.

Jev must never:
- turn a Sonar FAIL into PASS;
- change a Quality Gate;
- mark an issue false-positive/accepted automatically;
- change the release-evidence file.

## Rollout

1. Install and harden SonarQube on Mirin.
2. Establish private Aira -> Mirin route.
3. Install native SonarScanner on Aira.
4. Create one pilot Sonar project and project-scoped analysis token.
5. Run baseline analysis and approve baseline/new-code policy.
6. Enable official read-only Sonar MCP.
7. Enroll the pilot repository in Git MCP release-evidence enforcement.
8. Exercise PASS, FAIL, ERROR, STALE and remediation scenarios.
9. Enroll additional repositories individually.