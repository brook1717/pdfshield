"""
Application-wide rate-limiter instance.

Defined in its own module so both ``app.main`` (where it is wired into the
FastAPI app) and ``app.api.endpoints`` (where route decorators reference it)
can import it without creating a circular dependency.

Usage
-----
Route-level limit::

    from app.middleware.rate_limit import limiter

    @router.post("/upload")
    @limiter.limit("5/minute")
    async def upload(request: Request, ...):
        ...

Application wiring (``main.py``)::

    from app.middleware.rate_limit import limiter
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
"""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter: Limiter = Limiter(key_func=get_remote_address)
