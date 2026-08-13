"""Installable Control Plane package metadata.

Runtime implementation remains in :mod:`app` while the monolith is split in
reviewable work packages.  Keeping this package thin preserves every existing
``app.*`` import and the supported ``python -m app.main`` launcher.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
