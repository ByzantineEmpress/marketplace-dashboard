"""Adapter registry — lookup by platform name.

This is the central registry that maps platform names (e.g. 'ebay', 'etsy')
to their adapter classes. Plugins can register themselves here.
"""

from typing import Dict, Type

from src.adapters.base import MarketplaceAdapter

# Registry: platform_name -> adapter class
_ADAPTERS: Dict[str, Type[MarketplaceAdapter]] = {}


def register_adapter(platform: str, adapter_class: Type[MarketplaceAdapter]):
    """Register an adapter class for a given platform name.

    Call this during plugin loading.
    """
    if not issubclass(adapter_class, MarketplaceAdapter):
        raise TypeError(f"{adapter_class.__name__} must be a subclass of MarketplaceAdapter")
    _ADAPTERS[platform] = adapter_class


def get_adapter(platform: str) -> MarketplaceAdapter:
    """Return an instance of the adapter for *platform*.

    Raises KeyError if the platform is not registered.
    """
    if platform not in _ADAPTERS:
        raise KeyError(f"No adapter registered for platform '{platform}'. "
                       f"Available: {list(_ADAPTERS.keys())}")
    return _ADAPTERS[platform]()


def list_registered_platforms() -> list:
    """Return a list of all registered platform names."""
    return list(_ADAPTERS.keys())


# ------------------------------------------------------------------ #
#  Import core adapters at module load so they auto-register.
# ------------------------------------------------------------------ #

try:
    from src.adapters.ebay import eBayAdapter
    register_adapter("ebay", eBayAdapter)
except ImportError:
    pass

try:
    from src.adapters.etsy import EtsyAdapter
    register_adapter("etsy", EtsyAdapter)
except ImportError:
    pass

try:
    from src.adapters.poshmark import PoshmarkAdapter
    register_adapter("poshmark", PoshmarkAdapter)
except ImportError:
    pass

try:
    from src.adapters.amazon import AmazonAdapter
    register_adapter("amazon", AmazonAdapter)
except ImportError:
    pass

__all__ = ["register_adapter", "get_adapter", "list_registered_platforms"]
