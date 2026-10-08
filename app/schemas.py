"""Request bodies shared by the public and admin APIs."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.config import MAX_AMOUNT


class AuctionCreate(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    starting_price: int = Field(ge=1, le=MAX_AMOUNT)
    min_increment: int = Field(default=1, ge=1, le=MAX_AMOUNT)
    duration_seconds: int = Field(default=300, ge=5, le=7 * 24 * 3600)


class BidIn(BaseModel):
    # Ignored when the server requires login: the name comes from the account.
    bidder: str | None = Field(default=None, min_length=1, max_length=40)
    amount: int = Field(ge=1, le=MAX_AMOUNT)
    # Optional idempotency key. Send the same one when retrying a bid whose
    # response you never got, and you'll get the original outcome back.
    request_id: str | None = Field(default=None, min_length=1, max_length=64)
