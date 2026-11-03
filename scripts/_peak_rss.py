"""Peak resident memory for run records; Windows has no resource module, so there it is unknown."""
from __future__ import annotations

import sys

try:
    import resource
except ImportError:
    resource = None


def peak_rss_bytes(children: bool = False) -> int | None:
    """High-water mark of this process, or of its largest completed child; None when the platform cannot say."""
    if resource is None:
        return None
    rss = resource.getrusage(resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is bytes on macOS and KiB elsewhere.
    return rss if sys.platform == 'darwin' else rss*1024
