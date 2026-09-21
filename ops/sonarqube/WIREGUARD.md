# WireGuard contract: Aira -> Mirin SonarQube

Mirin is the WireGuard host/server. Aira is a peer/client.

## Addressing

Use one dedicated private subnet that does not overlap either site's LAN or another VPN. Example only:

```text
Mirin wg: 10.77.0.1/24
Aira  wg: 10.77.0.2/32
```

If the existing 10.8.0.0/24 deployment is reused, fix its routing/client reachability first rather than stacking another route for the same subnet.

## Exposure policy

After the tunnel is verified:

```text
SonarQube bind: <MIRIN_WG_IP>:9000
Aira SONAR_HOST_URL: http://<MIRIN_WG_IP>:9000
PostgreSQL: Docker-private only; never exposed over WireGuard
```

Do not use `0.0.0.0:9000`. Do not forward port 9000 on the Internet.

## Firewall contract

Allow:
- Aira WireGuard peer -> Mirin WireGuard address TCP/9000
- WireGuard UDP listen port from the Internet to Mirin, if Mirin is directly reachable/NAT-forwarded

Deny:
- arbitrary WireGuard peers -> SonarQube unless explicitly required
- any remote access to PostgreSQL port 5432/5433 for SonarQube

## Mirin server template

```ini
[Interface]
Address = 10.77.0.1/24
ListenPort = 51820
PrivateKey = <MIRIN_PRIVATE_KEY>

[Peer]
PublicKey = <AIRA_PUBLIC_KEY>
AllowedIPs = 10.77.0.2/32
```

## Aira client template

```ini
[Interface]
Address = 10.77.0.2/32
PrivateKey = <AIRA_PRIVATE_KEY>

[Peer]
PublicKey = <MIRIN_PUBLIC_KEY>
Endpoint = <MIRIN_REACHABLE_HOST_OR_IP>:51820
AllowedIPs = 10.77.0.1/32
PersistentKeepalive = 25
```

Keep `AllowedIPs` narrow: Aira needs only the Mirin WireGuard address for this service. A full-tunnel route is unnecessary.

## Activation checks

Before changing SonarQube off loopback:

1. Aira can reach Mirin's WireGuard IP.
2. TCP/9000 is unreachable before Sonar is rebound.
3. Recreate Sonar with `SONARQUBE_BIND_ADDRESS=<MIRIN_WG_IP>`.
4. Aira can query `/api/system/status` and receives `UP`.
5. Mirin LAN/public interfaces do not expose port 9000.
6. PostgreSQL remains Docker-private.
