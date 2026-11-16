"""Bounded memoization owned by one request, never shared across customers."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps

_CURRENT = ContextVar('rainbow_request_memo', default=None)


@contextmanager
def request_memo_scope():
    if _CURRENT.get() is not None:
        yield
        return
    token = _CURRENT.set({})
    try:
        yield
    finally:
        _CURRENT.reset(token)


def request_memoized(key_function):
    def decorate(function):
        @wraps(function)
        def run(*args, **kwargs):
            holder = _CURRENT.get()
            if holder is None:
                return function(*args, **kwargs)
            key = key_function(*args, **kwargs)
            if key is None:
                return function(*args, **kwargs)
            key = (function.__module__, function.__name__, key)
            if key in holder:
                return deepcopy(holder[key])
            result = function(*args, **kwargs)
            # Exceptions are never cached. Bound retained customer text and
            # results, release everything when this request finishes.
            if len(holder) < 64:
                holder[key] = deepcopy(result)
            return result
        return run
    return decorate
