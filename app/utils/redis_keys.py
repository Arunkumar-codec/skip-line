"""
app/utils/redis_keys.py
───────────────────────
Shared Redis key construction for the Phase 8 distributed rate limiter
(and any other cross-worker counters). Keys are prefixed to keep them
namespaced within whatever Redis database the app uses.
"""

PREFIX = "skipline:rl"


def fixed_window_key(bucket_key: str, window: float) -> tuple[str, int]:
    """Returns (redis_key, period) for a fixed-window counter.

    The bucket key is server-generated (an internal bucket id, an IP
    bucket id, or a principal id) — never raw client input beyond the IP
    the connection itself carries.
    """
    period = max(int(window), 1)
    return f"{PREFIX}:{bucket_key}", period
