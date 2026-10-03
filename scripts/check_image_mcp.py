#!/usr/bin/env python3
"""Read-only local MCP image transport check. Does NOT test model-visible pixels."""
from __future__ import annotations
import argparse
import base64
import getpass
import hashlib
from io import BytesIO
import json
import os
import sys
from PIL import Image
from smoke_mcp import Client, validate_url, LEGACY


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--workspace-id', required=True)
    parser.add_argument('--path', required=True, help='Exact allowed workspace-relative raster image')
    args = parser.parse_args()
    try:
        validate_url(args.url)
        token = os.environ.get('WORKSPACE_BRIDGE_TOKEN') or getpass.getpass('Shared bridge token: ')
        if not token or len(token) > 256 or '\n' in token or '\r' in token:
            raise ValueError('invalid token')
        client = Client(args.url, token)
        client.call('initialize', {'protocolVersion': LEGACY, 'capabilities': {},
                                  'clientInfo': {'name': 'workspace-bridge-image-check', 'version': '0.6.0'}})
        for modern in (False, True):
            result = client.call('tools/call', {'name': 'read_file', 'arguments': {
                'workspace_id': args.workspace_id, 'path': args.path, 'representation': 'image'}},
                                 modern=modern, max_response_bytes=3 * 1024 * 1024)
            blocks = result['content']
            if len(blocks) != 2 or [b['type'] for b in blocks] != ['text', 'image']:
                raise ValueError('no native image')
            meta = json.loads(blocks[0]['text']); image = blocks[1]
            if image['mimeType'] not in ('image/png', 'image/jpeg'):
                raise ValueError('unexpected format')
            data = base64.b64decode(image['data'], validate=True)
            if len(data) > 2 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != meta['preview_sha256']:
                raise ValueError('invalid image hash/size')
            with Image.open(BytesIO(data), formats=('PNG','JPEG')) as decoded:
                decoded.load()
                if decoded.size != (meta['width'],meta['height']):
                    raise ValueError('invalid image dimensions')
            print(f'PASS: {"modern" if modern else "legacy"} native image block; {meta["width"]}x{meta["height"]}, {len(data)} bytes')
        print('Local transport and decode passed; actual ChatGPT/tunnel visual recognition is NOT tested.')
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        print('Image check failed; inspect local configuration and safe server diagnostics. No credentials or image payload printed.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
