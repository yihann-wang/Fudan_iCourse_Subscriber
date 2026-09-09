"""Incomplete API responses that cannot be saved as finished notes."""


class TruncatedSummaryError(RuntimeError):
    """The response exhausted its output budget; it is not a finished note."""


class EmptySummaryResponseError(RuntimeError):
    """The service returned no answer; retry without changing the request."""
