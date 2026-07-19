from app.ratelimit import LoginThrottle


def test_allows_attempts_under_limit():
    throttle = LoginThrottle(max_attempts=3, window_seconds=900)
    throttle.record_failure()
    throttle.record_failure()
    assert throttle.retry_after_seconds() == 0


def test_locks_out_at_limit():
    throttle = LoginThrottle(max_attempts=3, window_seconds=900)
    for _ in range(3):
        throttle.record_failure()
    assert throttle.retry_after_seconds() > 0


def test_reset_clears_lockout():
    throttle = LoginThrottle(max_attempts=2, window_seconds=900)
    throttle.record_failure()
    throttle.record_failure()
    assert throttle.retry_after_seconds() > 0
    throttle.reset()
    assert throttle.retry_after_seconds() == 0


def test_window_expiry_allows_again():
    # A zero-length window means past failures never count against new attempts.
    throttle = LoginThrottle(max_attempts=1, window_seconds=0)
    throttle.record_failure()
    assert throttle.retry_after_seconds() == 0
