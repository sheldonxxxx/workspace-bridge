"""Notification-channel fakes for outbox tests."""

from workspace_bridge.notifications import NotificationEvent, NotificationResult


class RecordingChannel:
    """Channel fake recording safe semantic events rather than state kwargs."""
    channel_id = "recording"
    name = "Recording channel"
    enabled = True
    ready = True

    def __init__(self, result_factory=None):
        self.calls: list[NotificationEvent] = []
        self.result_factory = result_factory or (
            lambda event: NotificationResult(status="sent", attempts=1)
        )

    def deliver(self, event: NotificationEvent):
        self.calls.append(event)
        return self.result_factory(event)
