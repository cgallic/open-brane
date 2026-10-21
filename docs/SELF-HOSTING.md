# Self-host Open Brane

The smallest installation is one Python process and a directory of local files. The browser interface and Streamable HTTP MCP endpoint share the existing event writer and MCP tools. Your notes remain in SQLite; there is no hosted account or model subscription.

## Local installation

```bash
git clone https://github.com/cgallic/open-brane.git
cd open-brane
python3 brane.py setup --business personal
python3 brane.py serve
```

Python 3.11+ is required. Windows users can substitute `py -3.11` for `python3`. Setup creates `.venv`, installs `requirements-core.txt`, and creates `data/starter.json`, `data/events.db`, `data/auth.db`, `data/artifacts/`, and `data/http-token`. Paths in JSON resolve relative to that configuration file. `--business` applies only on first setup. Existing configuration and tokens are never overwritten.

The browser opens at http://127.0.0.1:7778. Read `data/http-token` and paste its contents into the unlock field. The browser keeps the token only in memory; reloading locks it. Use Save note, Search, or Recent events. Search is a literal substring search of stored payloads, with up to 50 results. It does not call an LLM. Entries are append-only; corrections should be saved as new notes.

`serve` stays in the foreground. Stop it with Ctrl+C. Change the port with `--port 8888`. Bind a network interface explicitly with `--host ADDRESS`; configure `public_origin` before accessing a custom hostname. Every HTTP API and MCP request requires the owner token, even when the customer connection setting `auth.enabled` is false. `/` contains only the locked app shell, and `/healthz` exposes liveness only.

For an existing installation:

```bash
python3 brane.py setup --config config/starter.json
python3 brane.py serve --config config/starter.json
python3 brane.py mcp-config --config config/starter.json
```

The token is created beside the chosen config. Existing database and artifact paths are preserved. Use `--config` consistently for that installation.

### Keep Python running on Linux

After setup, copy `systemd/brain-mcp.service`, replace `<BRAIN_ROOT>` with your absolute clone path and `<USER>` with the account owning it, then install the unit as `/etc/systemd/system/brain-mcp.service`:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now brain-mcp
sudo journalctl -u brain-mcp -n 100
```

This starts the authenticated server on localhost at boot. It uses the same `data/starter.json`; edit the unit if you chose another config. Front it with the HTTPS proxy described below for public access.

## Your everyday workspace

The home screen shows saved projects, saved next actions, documents, and recent activity. Open saved context or copy it into another session. See [Your workspace](WORKSPACE.md) for read-only HTTP/MCP discovery and deployment scope.

## Customer workflows

Use the browser Customer desk and the [customer handoff guide](CUSTOMER-HANDOFF.md) to connect normalized call/email observations. Run `python3 brane.py demo` for an isolated, two-client MCP demonstration. No provider credentials or messages are used.

## Agents

Generate stdio configuration from the installation directory:

```bash
python3 brane.py mcp-config
```

Copy the resulting `mcpServers` entry into a client that accepts this format, or enter its command and arguments in your client's MCP settings. Absolute paths make it independent of the client's working directory. Stdio launches its own process; it does not require the browser server. Do not expose that local process as an unauthenticated HTTP proxy.

For Streamable HTTP clients supporting custom headers:

- URL: `http://127.0.0.1:7778/mcp`
- Header: `Authorization: Bearer YOUR_ACCESS_TOKEN`
- Transport: Streamable HTTP

Behind HTTPS, substitute the public URL. No OAuth discovery or registration is implemented; clients requiring OAuth need a separate gateway. The token grants the tools full access to this deployment. Customer connection IDs provide provenance and scoped artifact operations, not independent HTTP user accounts.

Try asking your connected agent: “Save a note that our next launch is Friday,” then “Search my events for launch.” Browser notes and agent events appear in the same store.

## Docker on a laptop or VPS

```bash
docker compose -f compose.json up --build -d --wait
docker compose -f compose.json exec brane cat /data/http-token
```

The named `brane-data` volume holds the configuration and all persistent data under `/data`. The service initializes missing files on startup and runs as UID 10001. Do not run `down --volumes`: that deletes the stored brain. Bind mounts require a directory writable by UID 10001; the supplied named volume handles initial ownership automatically.

Useful commands:

```bash
docker compose -f compose.json logs --tail=100 brane
docker compose -f compose.json exec brane python /app/brane.py doctor --skip-install --config /data/starter.json
docker compose -f compose.json down
```

The Docker health check probes process liveness, not SQLite integrity. Use `doctor` to check the database.

For private access to a VPS without exposing a public service, tunnel its localhost port from your own machine:

```bash
ssh -L 7778:127.0.0.1:7778 your-user@your-server
```

Open http://127.0.0.1:7778 on your machine. The repository's Compose file intentionally publishes only the VPS's localhost port.

### Public HTTPS

Run a TLS reverse proxy on the VPS and route your domain to `127.0.0.1:7778`. Set `public_origin` in `/data/starter.json` to your HTTPS origin, for example `https://brain.example.com`, then restart the container. This also permits the domain through Host validation. A custom hostname that is not configured returns HTTP 400. Keep Bearer headers intact and allow streaming responses for `/mcp`.

One example for an existing Caddy installation:

```caddyfile
brain.example.com {
    reverse_proxy 127.0.0.1:7778
}
```

If your proxy is itself a container, connect it to the Compose network and proxy to `brane:7778` with the public Host header, instead of that container's localhost. Obtain HTTPS before sending owner tokens across the internet. No CORS access or cookie authentication is enabled.

### Token rotation

Stop the server, replace the `http-token` file with at least 32 random non-whitespace ASCII characters, then restart. For local Python:

```bash
python3 -c "from pathlib import Path; import secrets; Path('data/http-token').write_text(secrets.token_urlsafe(32) + '\n')"
```

Use a private file permission on Unix (`chmod 600 data/http-token`). HTTP processes also accept `BRAIN_HTTP_TOKEN` as an environment override; if set, it takes precedence over the file. Restart is required for a new token to take effect. Update your clients after rotation. Never commit either token or `.env` files.

## Backups and restore

A complete backup includes the event database, auth/checkpoint database, artifacts, configuration, and token. External attachment URIs can point outside this directory; back those attachments up separately. Do not copy only `events.db` while writers are running: committed data can still be in its WAL file.

For the local default setup, stop `serve` **and every stdio client, adapter, and other writer** first. Then archive the whole data directory:

```bash
python3 -c "import shutil; shutil.make_archive('brane-backup', 'gztar', 'data')"
```

For Docker, stop the service and any external writers, then create a full volume archive using a one-off container:

```bash
docker compose -f compose.json stop brane
docker compose -f compose.json run --rm --no-deps --entrypoint tar brane -czf - -C /data . > brane-backup.tar.gz
docker compose -f compose.json start brane
```

Keep the archive private: it contains your notes and access token. To restore locally, extract into an empty `data` directory with all writers stopped; then run `python3 brane.py doctor`. To restore Docker, use a fresh empty volume and pipe the archive into a one-off container before starting the service:

```bash
docker compose -f compose.json run --rm -T --no-deps --entrypoint tar brane -xzf - -C /data < brane-backup.tar.gz
docker compose -f compose.json up -d --wait
```

Restore into an empty volume, not over an existing brain. Test that a restored event and artifact can be read before relying on a backup.

## Upgrades

Back up first. Pull reviewed changes from your chosen branch, then:

```bash
# Python installation
python3 brane.py setup
python3 brane.py doctor
python3 brane.py serve

# Docker installation
docker compose -f compose.json up --build -d --wait
```

Setup is idempotent and only applies additive `CREATE ... IF NOT EXISTS` schemas. It does not reset data or import example notes. The container uses the same initialization path.

## Optional semantic search

The default is text search with vectors disabled. To enable semantic retrieval, follow [STARTER.md](STARTER.md): install `requirements.txt` into the same Python environment, run Ollama with your configured embedding model and Qdrant, and enable `vectors` in the JSON config. Then run the embedder explicitly or through your own scheduler:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/starter.py embed --config data/starter.json
python3 brane.py doctor
```

On Windows use `.venv\Scripts\python.exe` for the first two commands. The browser continues using text search. Docker ships the core only; vector support needs a custom image that installs the full requirements, reachable vector services, and an embedding schedule. No vector services are started automatically.

## Troubleshooting

| Symptom | Resolution |
|---|---|
| Setup cannot create a venv | Install your OS's Python venv/ensurepip support, then rerun setup. |
| Setup cannot download dependencies | Check outbound access to your configured Python package index. Retry setup; configuration is preserved. |
| Browser shows connection refused | Keep `serve` running, or inspect Compose logs. Check that another process has not taken port 7778. |
| HTTP 401 | Use the token beside this deployment's config. Check whether `BRAIN_HTTP_TOKEN` overrides it. |
| HTTP 400 on a domain | Set `public_origin` to that domain's HTTPS origin and restart. |
| Save rejected | Read the returned policy error. Blank notes, oversized payloads and known credential patterns are rejected. |
| Semantic search says unavailable | Vectors are optional and disabled by default. Use text search or configure the vector stack. |
| No web page after `starter.py run` | `run` is the stdio MCP transport. Use `brane.py serve` for the browser and HTTP. |
| Docker volume permission denied | Use the supplied named volume or make a bind-mounted directory writable by UID 10001. |
| Existing data seems missing | Check `--config` and the database path before initializing another deployment. |
