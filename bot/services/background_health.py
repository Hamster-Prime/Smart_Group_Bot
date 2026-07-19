from __future__ import annotations


MAX_CONSECUTIVE_BACKGROUND_FAILURES = 5


def record_background_failure(
    *,
    service: str,
    previous_failures: int,
    error: BaseException,
) -> int:
    """Surface a permanently broken runner to the process supervisor."""

    failures = max(0, int(previous_failures)) + 1
    if failures >= MAX_CONSECUTIVE_BACKGROUND_FAILURES:
        raise RuntimeError(
            f"{service} failed {failures} consecutive passes"
        ) from error
    return failures
