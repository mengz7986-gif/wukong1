"""Bounded compiled catalog expressions; never caches customer input/results."""
import re
from functools import lru_cache


@lru_cache(maxsize=8192)
def compiled(pattern, flags=0):
    return re.compile(pattern, flags)
