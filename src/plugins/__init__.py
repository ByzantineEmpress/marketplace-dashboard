"""Plugin discovery and loading system.

Plugins are Python modules placed in the ``plugins/`` directory.
Each plugin must be a single .py file that exports:

    PLUGIN_NAME   = "My Plugin"      # human-readable name
    PLUGIN_DESC   = "Does something cool"
    PLUGIN_VERSION = "1.0"

    def register(app):
        '''Register the plugin's routes and resources with the FastAPI app.'''
        ...

The system auto-discovers all ``*.py`` files in the plugins directory,
loads them, and calls ``register(app)`` if the function exists.

This is intentionally simple — no virtual environments, no dependencies,
no complex entry points. Just drop a file in ``plugins/`` and restart.
"""

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict, List


# Track loaded plugins for reporting
_LOADED_PLUGINS: List[Dict[str, str]] = []


def _get_plugin_dir() -> Path:
    """Return the path to the plugins directory."""
    from src.config import config
    plugin_dir = Path(config.PLUGIN_DIR)
    plugin_dir.mkdir(exist_ok=True)
    return plugin_dir


def discover_plugins(plugin_dir: Path = None) -> List[Path]:
    """Find all Python plugin files in the plugins directory.

    Returns a sorted list of ``.py`` file paths.
    Excludes ``__init__.py``, ``__pycache__``, and files starting with underscore.
    """
    if plugin_dir is None:
        plugin_dir = _get_plugin_dir()

    plugins = []
    for py_file in sorted(plugin_dir.glob("*.py")):
        if py_file.name.startswith("_") or py_file.name == "__init__.py":
            continue
        # Only consider it a plugin if it defines PLUGIN_NAME
        try:
            spec = importlib.util.spec_from_file_location("plugin", py_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                # Don't execute yet — just check if the file defines PLUGIN_NAME
                with open(py_file) as f:
                    content = f.read()
                if "PLUGIN_NAME" in content:
                    plugins.append(py_file)
        except Exception:
            pass  # Skip broken files

    return plugins


def load_plugin(plugin_path: Path) -> Dict[str, Any]:
    """Load and register a single plugin file.

    Returns a dict with the plugin metadata, or None if the plugin
    has no PLUGIN_NAME (not a valid plugin).
    """
    try:
        spec = importlib.util.spec_from_file_location(plugin_path.stem, plugin_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules[plugin_path.stem] = mod
            spec.loader.exec_module(mod)

            # Extract metadata
            plugin_info = {
                "name": getattr(mod, "PLUGIN_NAME", plugin_path.stem),
                "description": getattr(mod, "PLUGIN_DESC", ""),
                "version": getattr(mod, "PLUGIN_VERSION", "0.0"),
                "source": str(plugin_path),
            }

            # Call register() if it exists
            if hasattr(mod, "register"):
                from src.api.main import app
                mod.register(app)
                plugin_info["registered"] = True
            else:
                plugin_info["registered"] = False

            return plugin_info

    except Exception as e:
        return {
            "name": plugin_path.stem,
            "description": f"Error: {e}",
            "version": "error",
            "source": str(plugin_path),
            "error": str(e),
        }

    return None


def discover_and_load_plugins(app=None) -> List[str]:
    """Discover all plugins and load them.

    Returns a list of loaded plugin names.
    If *app* is provided, it's passed to each plugin's ``register()``.
    """
    from src.api.main import app as main_app
    _app = app or main_app
    _loaded = []
    _LOADED_PLUGINS.clear()

    plugins = discover_plugins()
    for plugin_path in plugins:
        info = load_plugin(plugin_path)
        if info and "name" in info:
            _loaded.append(info["name"])
            _LOADED_PLUGINS.append(info)

            # Register adapters if this plugin provides any
            from src.adapters import register_adapter
            if hasattr(sys.modules.get(plugin_path.stem, None), "register_adapter"):
                register_plugin_adapter(plugin_path.stem, _app)

    return _loaded


def register_plugin_adapter(module_name, app=None):
    """Helper: register an adapter from a plugin module.

    A plugin can define:
        ADAPTER_CLASS = MyAdapterClass

    And this function will register it with the adapter registry.
    """
    from src.adapters import register_adapter as reg
    import sys

    try:
        mod = sys.modules[module_name]
        adapter_class = getattr(mod, "ADAPTER_CLASS", None)
        if adapter_class:
            platform = getattr(adapter_class, "PLATFORM", module_name)
            reg(platform, adapter_class)
    except (KeyError, AttributeError):
        pass


def get_loaded_plugins() -> List[Dict[str, str]]:
    """Return metadata about all loaded plugins."""
    return _LOADED_PLUGINS.copy()


# Auto-load on import
def _auto_load():
    """Auto-discover and load plugins (manual trigger; see note below)."""
    discover_and_load_plugins()


# NOTE: not called at import time — the app's lifespan loads plugins once.
# Importing at module level would create a circular import with src/api/main.py.

__all__ = ["discover_plugins", "load_plugin", "discover_and_load_plugins",
           "get_loaded_plugins", "register_plugin_adapter"]
