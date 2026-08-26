"""Ingest-side read model: what devices actually shipped, and what did not.

``recorder/shipper.py`` posts signed batches outward; nothing in this package
read them back. This is that side — a tenant-scoped store of accepted evidence
plus the enrolment records needed to notice evidence that never arrived.

The distinction matters more than it sounds. A store built only from batches
can describe what it received; it cannot tell a device that has gone quiet
from a device that never existed. Only enrolment knows the difference, which
is why enrolment is a first-class table here rather than a side effect of the
first batch.
"""

from .store import AcceptResult, Enrolment, IngestStore

__all__ = ["IngestStore", "Enrolment", "AcceptResult"]
