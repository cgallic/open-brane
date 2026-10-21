#!/usr/bin/env python3
"""Push a normalized contact + interaction to an owner-authenticated deployment.

No messages are sent. The fixture uses unknown contact policy until an owner
provides a fresh, trusted policy. Only upload data you intend to retain.
"""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlencode, urlparse
from urllib.request import Request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:7778')
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--input', type=Path, default=Path(__file__).with_name('customer-handoff.json'))
    args = parser.parse_args()
    parsed = urlparse(args.url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}:
        parser.error('--url must be an HTTP(S) origin without credentials')
    if parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
        parser.error('remote deployments require HTTPS')
    token = args.token_file.read_text().strip() if args.token_file else os.environ.get('BRAIN_HTTP_TOKEN', '').strip()
    if not token:
        parser.error('provide --token-file or BRAIN_HTTP_TOKEN')
    body = json.loads(args.input.read_text())

    def call(path, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else None
        request = Request(args.url.rstrip('/') + path, data=raw,
                          headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        # Never forward owner credentials through HTTP redirects.
        from urllib.request import HTTPRedirectHandler, build_opener
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            return json.load(response)

    contact = call('/api/contacts', body['contact'])
    interaction = call('/api/interactions', body['interaction'])
    context = call('/api/customer-context?' + urlencode({'contact': contact['contact_id']}))
    print(json.dumps({'interaction_event_id': interaction['event_id'], 'deduped': interaction['deduped'],
                      'customer_context': context}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError) as exc:
        print(f'Handoff failed: {exc}', file=sys.stderr)
        sys.exit(1)
