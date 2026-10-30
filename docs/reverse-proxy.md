## Reverse proxy

Serve management at the **root of its own host**; path prefixes such as `/bridge/`
are not supported. To separate container-reachable audio/API traffic from local
administration:

```toml
host = "0.0.0.0"
port = 4759
public_url = "http://bridge.internal:4759"
management_host = "127.0.0.1"
management_port = 4760
management_public_url = "https://manage.example.test"
management_trusted_proxies = ["127.0.0.1", "::1"]
```

With `management_host` set, only add-on/audio routes exist on `host:port`; only the
page, assets and bearer CLI endpoint exist on the management listener. Both share
one native owner. Omit it to keep the original shared loopback listener.
`management_public_url` is a complete HTTP(S) origin without a path, query, fragment
or credentials. It controls browser POST origin checks and management redirects;
page links and forms use paths at that host's root. It is separate from `public_url`,
which continues to determine add-on/audio links only. An HTTPS management origin
sets Secure cookies even when the proxy connects to the bridge over HTTP.

Forward HTTPS through a private reverse proxy to `127.0.0.1:4760`, with VPN or firewall
restrictions and access logs disabled. For example, a Caddy site can contain:

```caddyfile
manage.example.test {
    reverse_proxy 127.0.0.1:4760
}
```

Arrange a trusted TLS certificate and restrict access in your proxy/network setup.
Keep Caddy access logging disabled. Preserve the public Host header and have the proxy
set/overwrite `X-Forwarded-Proto` and `X-Forwarded-For`. Trust only the proxy's actual
connection addresses using `management_trusted_proxies` (literal IPv4/IPv6 addresses;
no wildcard or network ranges). This enables the HTTPS request scheme and client
address for rate limits. Headers from every other address are ignored; forwarded Host
headers are never used. No proxy address is trusted by default, and
`FORWARDED_ALLOW_IPS` does not configure trust for the bridge's `serve` command.
Without `management_public_url`, origin checks use the request scheme and Host;
trusted forwarding still supplies the HTTPS scheme. Prefer an explicit public origin
behind a proxy.

The separate add-on API listener does not process management proxy headers. Use TLS
for any API traffic crossing an untrusted network too; paths contain tokens. A
container must reach `public_url`; loopback inside its namespace points to itself.

