"""Node pairing material: the Bridge gateway certificate, Node keys, signatures.

A paired Node dials the Bridge's Node gateway over TLS and pins the gateway's
self-signed certificate by SHA-256 fingerprint; there is no CA and no
hostname check. The Node proves its identity by signing a fresh gateway
challenge with its Ed25519 key. One-time pairing codes are stored only as
hashes. Nothing here touches the network.
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import os
import re
import secrets
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.x509.oid import NameOID

from .security import BridgeError

GATEWAY_CERT = "gateway-cert.pem"
GATEWAY_KEY = "gateway-key.pem"
NODE_KEY = "node-key.pem"
PAIRING_PREFIX = "wbpair1:"
GATEWAY_PATH = "/v1/node-gateway"
PAIRING_TTL_SECONDS = 600
PROTOCOL = 1
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_CODE = re.compile(r"[0-9a-f]{32}")


def _write_private(path: Path, data: bytes) -> bool:
    """Create a 0600 file exclusively; return False if it already exists."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return True


def _read_private(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        st = os.fstat(stream.fileno())
        if st.st_mode & 0o077 or st.st_uid != os.getuid():
            raise BridgeError("Pairing key material must be a private file (0600)",
                              "pairing_state_unsafe")
        return stream.read(64 * 1024)


def ensure_gateway_certificate(state: Path) -> tuple[Path, Path]:
    """Create the gateway's self-signed certificate once; return (cert, key) paths."""
    cert_path, key_path = state / GATEWAY_CERT, state / GATEWAY_KEY
    if not cert_path.exists():
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "workspace-bridge-gateway")])
        now = datetime.datetime.now(datetime.timezone.utc)
        certificate = (x509.CertificateBuilder()
                       .subject_name(name).issuer_name(name)
                       .public_key(key.public_key())
                       .serial_number(x509.random_serial_number())
                       .not_valid_before(now - datetime.timedelta(minutes=5))
                       .not_valid_after(now + datetime.timedelta(days=3650))
                       .add_extension(x509.SubjectAlternativeName(
                           [x509.DNSName("workspace-bridge-gateway")]), critical=False)
                       .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                       .sign(key, hashes.SHA256()))
        key_pem = key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
        # The key goes first: a certificate never exists without its key.
        if _write_private(key_path, key_pem):
            _write_private(cert_path, certificate.public_bytes(serialization.Encoding.PEM))
    for path in (cert_path, key_path):
        _read_private(path)
    return cert_path, key_path


def certificate_fingerprint(pem_or_der: bytes) -> str:
    """SHA-256 over the DER certificate, lowercase hex."""
    if pem_or_der.lstrip().startswith(b"-----BEGIN"):
        der = x509.load_pem_x509_certificate(pem_or_der).public_bytes(serialization.Encoding.DER)
    else:
        der = pem_or_der
    return hashlib.sha256(der).hexdigest()


def gateway_fingerprint(state: Path) -> str:
    cert_path, _ = ensure_gateway_certificate(state)
    return certificate_fingerprint(_read_private(cert_path))


def new_pairing_code() -> str:
    return secrets.token_hex(16)


def code_hash(code: str) -> str:
    return hashlib.sha256(("wb-pairing-v1:" + code).encode()).hexdigest()


def format_pairing(url: str, fingerprint: str, code: str) -> str:
    return f"{PAIRING_PREFIX}{url}#{fingerprint}.{code}"


def parse_pairing(value: str) -> tuple[str, str, str]:
    """Return (gateway URL, certificate fingerprint, one-time code)."""
    if not isinstance(value, str) or not value.startswith(PAIRING_PREFIX) or len(value) > 2200:
        raise BridgeError("Not a Workspace Bridge pairing string", "invalid_pairing")
    url, _, secret = value[len(PAIRING_PREFIX):].strip().rpartition("#")
    fingerprint, _, code = secret.partition(".")
    if (not url.startswith("wss://") or not url.endswith(GATEWAY_PATH)
            or not _FINGERPRINT.fullmatch(fingerprint) or not _CODE.fullmatch(code)
            or any(ord(char) < 33 for char in url)):
        raise BridgeError("Not a Workspace Bridge pairing string", "invalid_pairing")
    return url, fingerprint, code


def gateway_url(host: str, port: int) -> str:
    authority = f"[{host}]" if ":" in host else host
    return f"wss://{authority}:{port}{GATEWAY_PATH}"


# --------------------------------------------------------------- Node keys
def load_or_create_node_key(state: Path) -> Ed25519PrivateKey:
    path = state / NODE_KEY
    if not path.exists():
        key = Ed25519PrivateKey.generate()
        _write_private(path, key.private_bytes(serialization.Encoding.PEM,
                                               serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
    key = serialization.load_pem_private_key(_read_private(path), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise BridgeError("Node key is not an Ed25519 key", "pairing_state_unsafe")
    return key


def public_key_text(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def node_revision(public_key: str) -> str:
    """Connection revision of a paired Node: changes only when it is re-paired."""
    return "rev_" + hashlib.sha256(("wb-node-key:" + public_key).encode()).hexdigest()[:32]


def _release_line(version: object) -> tuple[int, ...] | None:
    match = re.match(r"(\d+)\.(\d+)", version) if isinstance(version, str) else None
    if match is None:
        return None
    major, minor = int(match[1]), int(match[2])
    return (major,) if major else (0, minor)


def compatible(node_version: object, bridge_version: str) -> bool:
    """Same major release (same minor before 1.0): the Node and Bridge speak the same requests."""
    line = _release_line(node_version)
    return line is not None and line == _release_line(bridge_version)


def _message(purpose: str, nonce: str, fingerprint: str, subject: str) -> bytes:
    return f"wb-node-v1|{purpose}|{nonce}|{fingerprint}|{subject}".encode()


def sign(key: Ed25519PrivateKey, purpose: str, nonce: str, fingerprint: str, subject: str) -> str:
    signature = key.sign(_message(purpose, nonce, fingerprint, subject))
    return base64.urlsafe_b64encode(signature).decode().rstrip("=")


def verify(public_key: str, signature: str, purpose: str, nonce: str,
           fingerprint: str, subject: str) -> bool:
    try:
        raw_key = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
        raw_signature = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if len(raw_key) != 32 or len(raw_signature) != 64:
            return False
        Ed25519PublicKey.from_public_bytes(raw_key).verify(
            raw_signature, _message(purpose, nonce, fingerprint, subject))
        return True
    except (InvalidSignature, ValueError, binascii.Error, TypeError):
        return False


def valid_public_key(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 43:
        return False
    try:
        return len(base64.urlsafe_b64decode(value + "=")) == 32
    except (ValueError, binascii.Error):
        return False


def pinned_client_context(certificate_pem: str) -> ssl.SSLContext:
    """TLS context that trusts exactly one self-signed gateway certificate."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cadata=certificate_pem)
    return context
