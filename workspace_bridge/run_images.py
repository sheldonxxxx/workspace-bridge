"""Small path-only run image references. The authoritative Node decodes bytes."""
from __future__ import annotations
import re
from .security import BridgeError

def validate_images(images) -> list[dict]:
    if images is None:
        return []
    if not isinstance(images, list) or len(images) > 4:
        raise BridgeError('Run images must contain at most four references', 'invalid_arguments')
    result = []
    for image in images:
        if not isinstance(image, dict) or set(image) - {'path', 'expected_sha256'}:
            raise BridgeError('Invalid run image reference', 'invalid_arguments')
        path = image.get('path')
        if not isinstance(path, str) or not path or len(path) > 4096 or '\0' in path:
            raise BridgeError('Invalid run image path', 'invalid_arguments')
        expected = image.get('expected_sha256')
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected)):
            raise BridgeError('Invalid run image hash', 'invalid_arguments')
        result.append({'path': path, **({'expected_sha256': expected} if expected else {})})
    return result
