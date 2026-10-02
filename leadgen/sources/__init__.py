"""The data sources (Google, Yelp, the free map data) and what they share."""

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any


class SourceError(RuntimeError):
    """A data source could not be queried (bad key, all servers down, ...)."""


# Where the listings a source has just returned go while a search runs, on the
# search's own thread (the site's search writes them to the database every few
# seconds, so a server restart mid-search keeps them: interrupted.py). None for the
# command line, the tests and the background fill-in.
_sink = threading.local()


@contextmanager
def collecting(sink: Callable[[list[Any]], None]) -> Iterator[None]:
    """While inside, report_found(leads) on this thread hands the leads to sink."""
    _sink.fn = sink
    try:
        yield
    finally:
        _sink.fn = None


def report_found(leads: list[Any]) -> None:
    """A source returned these listings (a results page, a map area); a no-op unless
    the search is collecting them (collecting)."""
    fn = getattr(_sink, "fn", None)
    if fn is not None and leads:
        fn(leads)
