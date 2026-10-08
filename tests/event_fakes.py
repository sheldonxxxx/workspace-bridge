"""Deterministic callback receiver; no network or ambient credentials."""
import base64
import json
from workspace_bridge.webhook_transport import CallbackError, CallbackResponse

TEST_SECRET = 'whsec_' + base64.b64encode(b'event-test-key-000000000000000000').decode()
TEST_URL = 'https://receiver.example/mcp-events/callback'


class CallbackReceiver:
    def __init__(self):
        self.verifications = []
        self.deliveries = []
        self.responses = []
        self.verification_error = None

    def verify(self, url, subscription_id, key):
        self.verifications.append((url, subscription_id, key))
        if self.verification_error:
            raise CallbackError(self.verification_error)

    def post(self, url, body, headers):
        self.deliveries.append({'body': body, 'event': json.loads(body), 'headers': headers, 'url': url})
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return CallbackResponse(response)
        return CallbackResponse(204)
