"""Plugin template — copy this to plugins/ to create a new plugin.

To create a plugin:
1. Copy this file to plugins/my_plugin.py
2. Edit PLUGIN_NAME, PLUGIN_DESC, PLUGIN_VERSION
3. Add your logic in the register() function
4. Restart the app

See README.md for more details.
"""

# ─── Plugin Metadata ───────────────────────────────────────────

PLUGIN_NAME = "Price Alerts"
PLUGIN_DESC = "Alert me when a listing's price changes"
PLUGIN_VERSION = "1.0"

# ─── Plugin Registration ───────────────────────────────────────

def register(app):
    """Register plugin routes and resources with the FastAPI app.

    This function is called automatically when the plugin is loaded.
    You can add API routes, templates, static files, etc.
    """
    from fastapi import APIRouter

    router = APIRouter()

    # Example: a simple stats endpoint
    @router.get("/plugin/price-alerts/stats")
    async def price_alert_stats():
        """Return alert statistics (stub — implement your logic)."""
        return {
            "alerted_listings": 0,
            "total_tracked": 0,
            "last_check": None,
        }

    # Example: register a custom CSS file
    # from fastapi.staticfiles import StaticFiles
    # os.makedirs("static/plugins/my_plugin", exist_ok=True)
    # app.mount("/static/plugins/my_plugin",
    #           StaticFiles(directory="static/plugins/my_plugin"),
    #           name="my_plugin_static")

    # Include the router
    app.include_router(router, prefix="/api/plugin")

    print(f"[plugin] {PLUGIN_NAME} v{PLUGIN_VERSION} registered")
