"""Passive eBay watcher for one specific item, with Telegram notifications."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ebay-sniper")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = ["__version__"]
