"""
Platform Abstraction Layer
===========================
Routes to platform_mac or platform_win based on sys.platform.
All other modules import from here instead of calling OS-specific APIs.
"""

import sys

if sys.platform == "win32":
    from .platform_win import *  # noqa: F401,F403
elif sys.platform == "darwin":
    from .platform_mac import *  # noqa: F401,F403
else:
    raise RuntimeError(f"Unsupported platform: {sys.platform}")
