"""
Vendor registry. Adding a camera vendor = new subpackage + entry here;
nothing in core/, gui/ or the remote layer changes.
"""

from .zwo import ZwoVendor
from .qhy import QhyVendor


def create_vendors() -> dict:
    """Fresh vendor instances, keyed by name, in enumeration order."""
    return {v.name: v for v in (ZwoVendor(), QhyVendor())}
