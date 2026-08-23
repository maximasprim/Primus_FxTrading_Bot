"""
Maps internal exceptions (provider failures, rate limits, data-quality
issues) to honest HTTP status codes, instead of routes catching
`Exception` and returning the same code for everything.

Before this existed: a Twelve Data 429, a network timeout, and a genuine
data-validation failure were all indistinguishable to the frontend --
either an uncaught 500 or a blanket 422 depending on which route you hit.
That made debugging (and any future frontend-side retry/backoff logic)
impossible to do correctly.
"""
import httpx
from fastapi import HTTPException

from app.data.market_data_engine import InvalidMarketDataError


def market_data_error_to_http(exc: Exception) -> HTTPException:
    """Classifies an exception raised anywhere in the market-data/analysis
    path and returns the HTTPException a route should raise for it.
    Callers should always `raise market_data_error_to_http(exc) from exc`.
    """
    if isinstance(exc, InvalidMarketDataError):
        return HTTPException(status_code=422, detail=f"Market data failed validation: {exc}")

    if isinstance(exc, httpx.HTTPStatusError):
        upstream_status = exc.response.status_code
        if upstream_status == 429:
            return HTTPException(
                status_code=429,
                detail="Market data provider rate limit reached. Please wait a moment and try again.",
            )
        if 500 <= upstream_status < 600:
            return HTTPException(
                status_code=502,
                detail=f"Market data provider returned a server error ({upstream_status}).",
            )
        return HTTPException(
            status_code=502,
            detail=f"Market data provider rejected the request ({upstream_status}).",
        )

    if isinstance(exc, httpx.RequestError):
        return HTTPException(status_code=503, detail="Could not reach the market data provider.")

    if isinstance(exc, ValueError):
        return HTTPException(status_code=500, detail=f"Configuration error: {exc}")

    return HTTPException(status_code=500, detail=f"Unexpected error: {exc}")