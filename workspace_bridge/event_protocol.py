"""Thin MCP 2.0 mapping for the OpenAI-documented webhook event contract."""
from __future__ import annotations

import asyncio
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from .event_broker import FINISHED, NEEDS_ATTENTION
from .security import BridgeError

EVENT_METHODS = frozenset({"events/list", "events/subscribe", "events/unsubscribe"})
WorkspaceID = Annotated[str, StringConstraints(pattern=r"^ws_[0-9a-f]{24}$")]
RunID = Annotated[str, StringConstraints(pattern=r"^run_[0-9a-f]{24}$")]


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EventList(EventInput):
    cursor: None = None


class EventFilters(EventInput):
    workspace_id: WorkspaceID
    run_id: RunID = Field(default=None)


class WebhookDestination(EventInput):
    mode: Literal["webhook"]
    url: str = Field(min_length=1, max_length=2048)


class SignedWebhookDestination(WebhookDestination):
    secret: str = Field(min_length=1, max_length=100)


class EventUnsubscribe(EventInput):
    name: Literal[FINISHED, NEEDS_ATTENTION]
    arguments: EventFilters
    delivery: WebhookDestination


class EventSubscribe(EventUnsubscribe):
    delivery: SignedWebhookDestination
    cursor: None = None
    ttlMs: int | None = Field(default=None, ge=1, le=2**53 - 1)


MODELS = {"events/list": EventList, "events/subscribe": EventSubscribe, "events/unsubscribe": EventUnsubscribe}


def arguments(method: str, params: dict) -> dict:
    try:
        value = MODELS[method].model_validate({key: value for key, value in params.items() if key != "_meta"}).model_dump()
        if "arguments" in value:
            value["arguments"] = {key: item for key, item in value["arguments"].items() if item is not None}
        return value
    except ValidationError:
        raise BridgeError("Invalid event arguments", "invalid_event_arguments") from None


async def dispatch(broker, method: str, token: str, args: dict) -> dict:
    if method == "events/list":
        return await asyncio.to_thread(broker.catalog, token)
    if method == "events/subscribe":
        return await asyncio.to_thread(broker.subscribe, token, args["name"], args["arguments"], args["delivery"],
                                       ttl_ms=args["ttlMs"], cursor=args["cursor"])
    return await asyncio.to_thread(broker.unsubscribe, token, args["name"], args["arguments"], args["delivery"])
