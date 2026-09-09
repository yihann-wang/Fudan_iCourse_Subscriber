"""The note returned by a single whole-transcript request."""

from typing import NamedTuple


class SummaryResult(NamedTuple):
    text: str
    model: str
