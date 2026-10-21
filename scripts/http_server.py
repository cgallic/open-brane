#!/usr/bin/env python3
"""Owner-only browser UI and Streamable HTTP MCP, sharing the canonical tools."""
import argparse
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from urllib.parse import urlparse

from runtime_config import apply_config


def create_app(config_path):
    # Apply before importing tools: their DB/owner paths are module-level constants.
    config = apply_config(config_path)
    token = os.environ.get('BRAIN_HTTP_TOKEN', '').strip()
    if not token:
        token = (Path(config_path).resolve().parent / 'http-token').read_text().strip()
    if len(token) < 32 or not token.isascii() or any(ch.isspace() for ch in token):
        raise ValueError('HTTP access token must contain at least 32 non-whitespace ASCII characters; run brane.py setup')

    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route
    from starlette.concurrency import run_in_threadpool
    from mcp.server.transport_security import TransportSecuritySettings
    import mcp_server as brain

    async def home(request):
        return HTMLResponse((Path(__file__).parent / 'ui.html').read_text(), headers={
            'Content-Security-Policy': "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-store',
            'Referrer-Policy': 'no-referrer',
        })

    async def live(request):
        # Liveness only. Never reveal event counts, configuration or database paths.
        return JSONResponse({'status': 'ok'})

    async def events(request):
        try:
            if request.method == 'POST':
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 262144:
                        return JSONResponse({'error': 'Note exceeds 256 KiB'}, status_code=413)
                body = json.loads(raw)
                summary = body.get('summary') if isinstance(body, dict) else None
                if not isinstance(summary, str) or not summary.strip():
                    raise ValueError('Write a note before saving')
                result = await run_in_threadpool(brain.record_event, source='manual', type='note', payload={'summary': summary.strip()})
                return JSONResponse(result, status_code=400 if result.get('status') == 'error' else 201)
            query = request.query_params.get('q', '').strip()
            rows = await run_in_threadpool(brain.search_events, query=query, limit=50) if query else await run_in_threadpool(brain.query_events, limit=50)
            return JSONResponse(rows)
        except (ValueError, sqlite3.Error) as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)

    # Outer Host validation covers UI, REST, and MCP. A configured public origin
    # permits a reverse-proxy domain without accepting arbitrary Host headers.
    public = urlparse(config.get('public_origin', 'http://127.0.0.1'))
    hosts = ['localhost', '127.0.0.1', '[::1]']
    if public.hostname and public.hostname not in hosts:
        hosts.append(public.hostname)
    brain.mcp.settings.stateless_http = True
    brain.mcp.settings.json_response = True
    brain.mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = brain.mcp.streamable_http_app()
    app.router.routes.extend([Route('/', home), Route('/healthz', live), Route('/api/events', events, methods=['GET', 'POST'])])
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts, www_redirect=False)

    class OwnerAuth:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope['type'] == 'http':
                headers = dict(scope['headers'])
                # Bearer auth applies to every private route and every MCP method.
                if scope['path'] not in ('/', '/healthz'):
                    actual = headers.get(b'authorization', b'')
                    expected = ('Bearer ' + token).encode('ascii')
                    if not secrets.compare_digest(actual, expected):
                        response = JSONResponse({'error': 'Access token required'}, status_code=401,
                                                headers={'WWW-Authenticate': 'Bearer', 'Cache-Control': 'no-store'})
                        await response(scope, receive, send)
                        return
            async def no_cache(message):
                if message['type'] == 'http.response.start':
                    message.setdefault('headers', []).append((b'cache-control', b'no-store'))
                await send(message)
            await self.app(scope, receive, no_cache)

    app.add_middleware(OwnerAuth)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=7778)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.config), host=args.host, port=args.port, access_log=False, proxy_headers=False)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f'Cannot start HTTP server: {exc}', file=sys.stderr)
        sys.exit(1)
