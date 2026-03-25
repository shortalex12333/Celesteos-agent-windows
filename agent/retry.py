"""
Retry decorator with exponential backoff + jitter.
Designed for satellite / unreliable network links.
"""

import functools
import logging
import random
import time

import requests

logger = logging.getLogger("agent.retry")

# HTTP status codes that should trigger a retry
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 8.0):
    """
    Decorator that retries on requests.RequestException and retryable HTTP status codes.

    Uses exponential backoff with jitter: delay * (0.5 + random()).

    Args:
        max_retries: Maximum number of retries (not counting the initial attempt)
        base_delay: Base delay in seconds (doubles each retry)
        max_delay: Maximum delay cap in seconds
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_retries + 1):
                try:
                    result = func(*args, **kwargs)

                    # Check if result is a requests.Response with a retryable status
                    if isinstance(result, requests.Response) and result.status_code in RETRYABLE_STATUS_CODES:
                        if attempt < max_retries:
                            delay = min(base_delay * (2 ** attempt), max_delay)
                            jittered = delay * (0.5 + random.random())
                            logger.warning(
                                "Retryable HTTP %d from %s (attempt %d/%d), retrying in %.1fs",
                                result.status_code, func.__name__, attempt + 1, max_retries + 1, jittered,
                            )
                            time.sleep(jittered)
                            continue
                        # Final attempt — return the response as-is
                        return result

                    return result

                except requests.RequestException as exc:
                    last_exc = exc
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        jittered = delay * (0.5 + random.random())
                        logger.warning(
                            "%s in %s (attempt %d/%d), retrying in %.1fs: %s",
                            type(exc).__name__, func.__name__, attempt + 1, max_retries + 1, jittered, exc,
                        )
                        time.sleep(jittered)
                    else:
                        raise

            # Should not reach here, but just in case
            if last_exc:
                raise last_exc

        return wrapper

    return decorator
