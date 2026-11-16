"""Ordinary admin business readiness is independent of password length.

Authentication, administrator authorization and CSRF remain route guards.
This predicate only refuses an unsafe session signing secret.
"""


def ordinary_business_ready(session_secret):
    return (
        isinstance(session_secret, str)
        and len(session_secret) >= 32
        and session_secret != 'SESSION_SECRET_KEY'
    )
