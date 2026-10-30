## CLI management

```sh
uv run mornye-bridge rotate-tokens
uv run mornye-bridge registry get
uv run mornye-bridge registry set "$REGISTRY_URL"
uv run mornye-bridge available --refresh
uv run mornye-bridge update EXTENSION
uv run mornye-bridge enable EXTENSION
uv run mornye-bridge disable EXTENSION
uv run mornye-bridge remove EXTENSION
uv run mornye-bridge describe EXTENSION
uv run mornye-bridge settings show EXTENSION
uv run mornye-bridge settings set EXTENSION --input /private/settings.json
uv run mornye-bridge actions EXTENSION
uv run mornye-bridge action EXTENSION ACTION
uv run mornye-bridge health EXTENSION
uv run mornye-bridge cancel EXTENSION ENCODED_TRACK_ID --format NATIVE_FORMAT_ID
```

`rotate-tokens` replaces the add-on access and CLI admin tokens through the running
owner or the exclusive offline owner. Recopy every Shijhon add-on link afterward.
It preserves the master key, native settings and browser password/sessions; use
`set-password` to revoke browser sessions.

Settings input is a JSON object; supplied keys merge with existing settings. `--input -`
reads stdin. Settings values are redacted unless the manifest explicitly marks the
field public (booleans/numbers are public unless marked secret); sensitive names such
as tokens are always hidden, including unmarked media tokens. `describe` supplies
setting schema and native format options for a future UI, without setting defaults
that might contain secrets. `actions` lists manifest action buttons. Health reports
native probe statuses, not proof that a particular track will play.

An action's returned `setting_updates`, including nested managed-action results, are
merged into extension settings. For multi-step `action_form`/`byoa_form` login, request
the private result, read its submit/cancel action, and submit a JSON **argument array**
(e.g. `[{"field":"value"}]`) using `action EXTENSION SUBMIT_ACTION --input FILE`.
Form input is transient and is not saved as settings. Use a fresh private output file
for each step; invoke the form's cancel action to abandon it. The CLI does not launch
a browser or automatically run a form loop.

```sh
uv run mornye-bridge --private-output /private/new-action-result.json action EXTENSION login
uv run mornye-bridge pending-auth show EXTENSION
uv run mornye-bridge --private-output /private/new-auth-result.json pending-auth show EXTENSION
# Open the private auth_url in your browser. Keep its callback/grant private.
uv run mornye-bridge pending-auth grant EXTENSION --input /private/grant.txt
uv run mornye-bridge pending-auth callback EXTENSION --input /private/callback-url.txt
```

`--private-output` creates a new 0600 file and refuses to overwrite one. It is the
explicit path for sensitive operator results; ordinary action output hides strings.
Use private files or stdin rather than putting credentials in command-line arguments.
The host validates callback ownership/state and completes native grants/OAuth; callback
submission alone does not prove success. Check pending auth, then retry the failed
operation after completing login. OAuth callback routing is whatever this pinned
host supports, including its specifically named login completion action; the bridge
adds no provider-specific OAuth router. Some flows expire within three minutes.

Use `mornye-bridge --config /path/to/config.toml COMMAND`; the config option goes
before the command. Prefix with `uv run` in the source checkout. `check` initializes
the native library and enumerates extensions. With a running server it uses the
bearer-authenticated loopback management endpoint; offline it takes an exclusive
state lock and starts a temporary worker. Never run a second state owner.
The management listener must accept IPv4 loopback for CLI connections.
