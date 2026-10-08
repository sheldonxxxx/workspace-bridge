"""Wire signatures, challenge verification and DNS-pinned SSRF boundaries."""
import base64
import hashlib
import hmac
import json
import socket

import pytest
from workspace_bridge.webhook_transport import (
    CallbackError, CallbackResponse, WebhookTransport, callback_url, public_addresses, signed_headers, signing_key,
)
from event_fakes import TEST_SECRET, TEST_URL


@pytest.mark.parametrize('url', ['http://example.com/cb', 'https://u:p@example.com/cb',
    'https://example.com/cb#secret', 'https://example.com:0/cb', 'https://example.com:65536/cb',
    'https://example.com/\nsecret', 'https://example.com/\\secret', 'https://[fe80::1%25en0]/cb', 'https:///missing'])
def test_url_rejects_unsafe_shapes_without_echo(url):
    with pytest.raises(CallbackError) as error:
        callback_url(url)
    assert error.value.reason == 'invalid_url' and url not in str(error.value)


@pytest.mark.parametrize('secret', ['whsec_', 'secret', 'whsec_bad',
    'whsec_' + base64.b64encode(b'x' * 23).decode(), 'whsec_' + base64.b64encode(b'x' * 65).decode()])
def test_signing_key_lengths_and_base64(secret):
    with pytest.raises(CallbackError) as error:
        signing_key(secret)
    assert error.value.reason == 'invalid_secret'


def test_standard_webhooks_signature_covers_exact_body_id_and_timestamp():
    key = signing_key(TEST_SECRET)
    body = b'{"eventId":"evt_test","data":{}}'
    headers = signed_headers('sub_test', 'evt_test', body, key, timestamp=1791072000)
    expected = base64.b64encode(hmac.new(key, b'evt_test.1791072000.' + body, hashlib.sha256).digest()).decode()
    assert headers['webhook-signature'] == 'v1,' + expected
    assert headers['webhook-timestamp'] == '1791072000' and headers['webhook-id'] == 'evt_test'
    assert headers['X-MCP-Subscription-Id'] == 'sub_test'
    assert signed_headers('sub_test', 'evt_test', body + b' ', key, timestamp=1791072000)['webhook-signature'] != headers['webhook-signature']


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '192.168.1.1', '172.16.0.1', '169.254.169.254',
    '100.64.0.1', '0.0.0.0', '224.0.0.1', '192.0.2.1', '::1', 'fc00::1', 'fe80::1', '::ffff:127.0.0.1',
    '2002:7f00:0001::1'])
def test_dns_rejects_non_public_and_transition_addresses(monkeypatch, address):
    family = socket.AF_INET6 if ':' in address else socket.AF_INET
    sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', sockaddr)])
    with pytest.raises(CallbackError) as error:
        public_addresses('receiver.example', 443)
    assert error.value.reason == 'invalid_destination'


def test_dns_mixed_public_private_and_rebinding_are_rejected(monkeypatch):
    answer = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443))]
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: list(answer))
    assert public_addresses('receiver.example', 443)
    answer.append((socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443)))
    with pytest.raises(CallbackError):
        public_addresses('receiver.example', 443)


def test_post_pins_ip_preserves_tls_name_and_never_follows_redirect(monkeypatch):
    calls = {'dns': [], 'connect': [], 'tls': [], 'request': []}
    def dns(host, port, **kwargs):
        calls['dns'].append((host, port))
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', port))]
    class Sock:
        def settimeout(self, timeout): pass
        def connect(self, address): calls['connect'].append(address)
        def close(self): pass
    class Context:
        def wrap_socket(self, sock, server_hostname):
            calls['tls'].append(server_hostname)
            return sock
    class Response:
        status = 302
        def read(self, limit): return b''
    class Connection:
        sock = None
        def __init__(self, *args, **kwargs): pass
        def request(self, method, path, body, headers): calls['request'].append((method, path, body, headers))
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setattr(socket, 'getaddrinfo', dns)
    monkeypatch.setattr(socket, 'socket', lambda *a: Sock())
    monkeypatch.setattr('workspace_bridge.webhook_transport.ssl.create_default_context', lambda: Context())
    monkeypatch.setattr('workspace_bridge.webhook_transport.http.client.HTTPSConnection', Connection)
    transport = WebhookTransport()
    assert transport.post(TEST_URL, b'{}', {}).status == 302
    assert calls['dns'] == [('receiver.example', 443)]
    assert calls['connect'] == [('93.184.216.34', 443)] and calls['tls'] == ['receiver.example']
    assert len(calls['request']) == 1
    transport.post(TEST_URL, b'{}', {})
    assert len(calls['dns']) == 2  # fresh destination validation on each connection


def test_callback_challenges_are_signed_unique_and_single_use(monkeypatch):
    transport = WebhookTransport()
    calls = []
    def post(url, body, headers, **kwargs):
        value = json.loads(body)
        assert value['type'] == 'verification'
        assert headers['webhook-id'].startswith('msg_verification_')
        assert headers['webhook-signature'] == signed_headers('sub_test', headers['webhook-id'], body,
            signing_key(TEST_SECRET), timestamp=int(headers['webhook-timestamp']))['webhook-signature']
        calls.append((body, headers))
        return CallbackResponse(200, json.dumps({'challenge': value['challenge']}).encode())
    monkeypatch.setattr(transport, 'post', post)
    transport.verify(TEST_URL, 'sub_test', signing_key(TEST_SECRET))
    transport.verify(TEST_URL, 'sub_test', signing_key(TEST_SECRET))
    assert calls[0][0] != calls[1][0] and calls[0][1]['webhook-id'] != calls[1][1]['webhook-id']
    monkeypatch.setattr(transport, 'post', lambda *a, **k: CallbackResponse(200, json.dumps({'challenge': json.loads(calls[0][0])['challenge']}).encode()))
    with pytest.raises(CallbackError) as error:
        transport.verify(TEST_URL, 'sub_test', signing_key(TEST_SECRET))
    assert error.value.reason == 'challenge_failed'


@pytest.mark.parametrize('response', [CallbackResponse(302, b'{}'), CallbackResponse(200, b'bad-json'),
    CallbackResponse(200, b'{"challenge":"wrong"}'), CallbackResponse(200, b'{"challenge":"\\u4e2d"}')])
def test_invalid_challenge_never_activates_delivery(monkeypatch, response):
    transport = WebhookTransport()
    monkeypatch.setattr(transport, 'post', lambda *args, **kwargs: response)
    with pytest.raises(CallbackError):
        transport.verify(TEST_URL, 'sub_test', signing_key(TEST_SECRET))
