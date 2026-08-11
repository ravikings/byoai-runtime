"""Shared test wiring for exercising MockCoriqo over a real ASGI transport."""

from __future__ import annotations

import httpx
from starlette.testclient import TestClient

from .mock_coriqo import MockCoriqo


def asgi_client(mock: MockCoriqo, *, base_url: str = "http://mock-coriqo.test") -> httpx.Client:
    """A real httpx.Client (well, an httpx.Client subclass) wired to the
    mock's ASGI app — no sockets.

    The installed httpx version's ``ASGITransport`` only implements the
    async request path (``handle_async_request``), so a plain sync
    ``httpx.Client(transport=httpx.ASGITransport(...))`` raises
    ``AttributeError`` the moment it's used. Starlette's ``TestClient`` is a
    thin ``httpx.Client`` subclass that bridges sync calls onto an ASGI app
    correctly, and satisfies the ``http_client: httpx.Client`` parameter
    both ``enroll()`` and ``Shipper`` accept.

    Entering it as a context manager runs the ASGI app's lifespan
    startup/shutdown — MockCoriqo has none today, but callers should still
    use it via ``with`` so that stays true if it ever grows any.
    """
    return TestClient(mock.app, base_url=base_url)
