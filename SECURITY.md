# Security

`opencode-go-proxy` is an independent, single-user local adapter between Codex
and user-selected model providers. It is not affiliated with, sponsored by, or
endorsed by OpenAI or OpenCode.

The project is not an account-sharing service. It does not provide pooled
accounts, shared credentials, subscription resale, or rate-limit
circumvention. Users must use accounts and credentials they are authorized to
use and remain responsible for current provider terms and policies.

## Secrets

The proxy never needs credentials committed to the repository. It resolves the
upstream API key in this order:

1. The configured environment variable, defaulting to `OPENCODE_GO_API_KEY`.
2. The macOS keychain entry `opencode-go-api-key` (override with `CODEX_KEYCHAIN_SERVICE`).

Only credential-source metadata is traced. Credential values are retained only
in process memory and are not written to the proxy's meter, trace, catalog, or
support bundle.

Native OpenAI requests are a separate path. If a requested model is in the
captured native Codex catalog, the proxy relays the client's existing
authorization to the configured native HTTPS endpoint. It does not store that
authorization, convert it into an OpenCode credential, or send the OpenCode
key to the native endpoint. Only set `OPENCODE_GO_PROXY_NATIVE_BASE_URL` to an
endpoint you trust with that native authorization.

## Network exposure

The default bind is `127.0.0.1`. Requests are checked against both their `Host`
header and the actual socket peer address, and browser-originated requests are
rejected.

A non-loopback bind fails closed unless:

1. `OPENCODE_GO_PROXY_ALLOW_REMOTE=1` is set.
2. `OPENCODE_GO_PROXY_CALLER_TOKEN` contains at least 32 characters.
3. Every non-loopback client sends that value in
   `X-OpenCode-Go-Proxy-Token`.

The caller token protects access to the proxy; it is not an upstream provider
credential. The proxy does not forward `X-OpenCode-Go-Proxy-Token` to native or
routed providers. Use a random token, TLS, and network-level access controls
for any deliberate remote deployment. Do not expose a plaintext listener to
the public internet.

For OpenCode routes, the proxy constructs upstream authorization from the
user-owned environment or keychain credential; it does not forward the
client's bearer token. Native relaying has an explicit header allowlist and
excludes hop-by-hop and proxy-control headers.

## SSRF protection

Image URLs in conversation content are validated — only `data:image/` and
`https://` schemes are allowed. `file://`, `http://`, `ftp://`, and other
schemes are rejected to prevent server-side request forgery.

The proxy does not fetch image URLs itself — it forwards them to the upstream
Chat Completions API, which is responsible for fetching and processing them.
The scheme check prevents the proxy from passing `file://` or `http://` URLs
that could be used to probe internal services via the upstream.

## Reports

Open a private security advisory or contact the maintainers before publishing a
bug report involving credentials, prompts, tool outputs, or request traces.
Never include live credentials or authorization headers in a report.
