# Git history configuration audit

Date: 2026-09-22

## Scope and method

Scanned every reachable commit (`git log --all`) in Fleet, Studio, Gateway,
Filesystem, and Git MCP. Patterns covered machine home and private-runtime paths,
tunnel identifiers, OpenAI-style keys, bearer credentials, and
`CONTROL_PLANE_API_KEY`. History object paths were also checked for credential,
secret, token, and environment files. The scan emitted only repository, commit, path,
and classification; it did not print candidate values or open credential files.

## Findings

| Classification | Repository | First introducing commit | Historical path/context | Published tags containing commit | Disposition |
| --- | --- | --- | --- | --- | --- |
| `HOST_LOCAL_METADATA` | Fleet | `55960fd95679` | `hosts/aira.toml` machine-local paths | `v0.1.0`, `v0.2.0`, `v0.2.1`, `v0.3.0`, `v0.3.1`, `v0.3.2` | Remove from current source; retain runtime authority. |
| `SENSITIVE_INFRA_METADATA` | Fleet | `b03fd4902882` | `hosts/aira.toml` tunnel identity | `v0.3.1`, `v0.3.2` | Remove from current source; preserve host-local runtime identity. |
| `SAFE_TEMPLATE` | Fleet | `f0150b4e5d9c` | `scripts/fleetctl.py`, tests: `CONTROL_PLANE_API_KEY` as `from_file` reference | `v0.3.2` | No secret value; retain reference-only design. |
| `FALSE_POSITIVE` | Studio | `3aa9db2c6e65` | storage test asserts non-persistence of synthetic private paths | `v0.6.0-alpha`, `v0.6.0`, `v0.7.0-beta` | Test-only non-production path. |
| `SAFE_TEMPLATE` | Studio | `097102e55a8f` | tunnel config uses `CONTROL_PLANE_API_KEY` file reference | `v0.6.0`, `v0.7.0-beta` | No literal assignment detected. |
| `HOST_LOCAL_METADATA` | Gateway | `d8bd65ba903e` | historical README and generated server definitions | `v0.1.0`, `v0.2.0` | Historical metadata; separately remediate Gateway source if still present. |
| `FALSE_POSITIVE` | Gateway | `cdfac2ea9709` | test-only private temporary path | `v0.2.0` | Test fixture, not host authority. |
| `HOST_LOCAL_METADATA` | Git MCP | `2e9666ed6ab6` | historical README path | `v0.1.0` | Historical metadata; separately remediate documentation if still present. |

Additional broad `API_KEY`, `TOKEN`, `SECRET`, and `PASSWORD` assignment-marker
matches were classified `FALSE_POSITIVE`: Fleet release-workflow references
(`d94f77146529`, `14e639ea08d4`), Fleet Sonar bootstrap identifiers
(`57d0836312cb`), Studio documentation/template and release-workflow references
(`c0e9fb26d83a`, `4bde0f30db3a`, `ba41cdfb7ed4`, `3aa9db2c6e65`), Gateway
release-workflow references (`472c78cec1e0`, `02a46af100c0`), Filesystem
release-workflow references (`5165e8741a7d`, `0f09a665bb94`), and Git MCP
release-workflow references (`a09d8d1b184`, `09bfca39a7d9`). A second scan for
quoted literal assignments found no matches. Values were not printed.

No match was found for an OpenAI-style API key, bearer credential, or credential-like
file path in the scanned reachable history. No actual control-plane API-key value was
found. `CONTROL_PLANE_API_KEY` matches above were verified as file-reference code,
not literal credential assignments.

## Current remediation result

Fleet runtime host identity now belongs only under `runtime/fleet/hosts`. Fleet source
contains examples, renderer logic, validators, and the source-hygiene check; it does
not retain the Aira production profile. The hygiene check rejects machine home paths,
tunnel identifiers outside tests/examples, credential-bearing tracked files, and common
credential literals.

## History-remediation decision

Recommendation: **Option A — forward-only remediation**.

Published tags contain host-local paths and tunnel metadata but no actual secret value.
Rewriting would require force-pushing protected history, recreating affected tags and
GitHub Releases, invalidating downstream clones and provenance/checksums, and checking
Studio release metadata plus update-provider behavior. Those costs exceed the benefit
for non-secret metadata. Keep this audit with release records, ship the forward fix in
a new Fleet release when approved, and do not rotate credentials.

Use **Option B — history rewrite** only if a future audit finds an actual credential
value or policy requires metadata erasure. Before any rewrite, rotate exposed
credentials, inventory release artifacts/checksums and downstream clones, recreate
release tags deliberately, and coordinate Studio update metadata. This task does not
rewrite history, force-push, change remote tags, or alter releases.
