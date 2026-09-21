# Writing Plugins

A plugin is a single `.py` file dropped into the `plugins/` folder. The
server discovers them at startup (or when you restart) — no registry, no
build step, no separate environment.

## The contract

```python
PLUGIN_NAME = "My Plugin"          # shown in Admin → Plugins
PLUGIN_DESC = "What it does"        # one line
PLUGIN_VERSION = "1.0"             # free-form

def register(app):
    """Called once at startup with the FastAPI app.
    Add routes, templates, background jobs — anything."""
```

That's the whole interface. `register(app)` is optional (a plugin without
it is just a passive module), but it's how plugins add behaviour.

### Bonus hook: shipping a marketplace adapter

If your plugin adds a **new marketplace**, set `ADAPTER_CLASS` to a
`MarketplaceAdapter` subclass and register it in `register(app)`:

```python
from src.adapters.base import MarketplaceAdapter
from src.adapters import register_adapter

class MyPlatformAdapter(MarketplaceAdapter):
    PLATFORM = "myplatform"
    PLATFORM_LABEL = "My Platform"
    # implement the adapter interface (OAuth + sync) ...

PLUGIN_NAME = "My Platform"
PLUGIN_DESC = "Connect to My Platform"
PLUGIN_VERSION = "1.0"
ADAPTER_CLASS = MyPlatformAdapter

def register(app):
    register_adapter("myplatform", MyPlatformAdapter)
```

That's how Poshmark / Facebook Marketplace / Mercari / Depop are meant to
be added — as plugins, not core code.

## Full example — a plugin with an API route

Create `plugins/slow_listings.py`:

```python
from fastapi import APIRouter

PLUGIN_NAME = "Slow Listings"
PLUGIN_DESC = "Shows the listings that have never sold"
PLUGIN_VERSION = "1.0"

def register(app):
    from sqlalchemy import text
    from src.database import SessionLocal

    router = APIRouter()

    @router.get("/plugin/slow-listings/top")
    async def slowest(limit: int = 10):
        with SessionLocal() as db:
            rows = db.execute(
                text("SELECT * FROM listings ORDER BY created_at LIMIT :n"),
                {"n": limit},
            ).fetchall()
        return [dict(r._mapping) for r in rows]

    app.include_router(router, prefix="/api")
```

Restart the server → the route is live at `/api/plugin/slow-listings/top`
and the plugin shows in **Admin → Plugins**.

## Rules of thumb

- **Use the app's models** — import from `src.models` instead of opening
  your own DB connection; you share the same engine and schema.
- **Prefix your routes** with `/plugin/<your-name>/...` so you never
  collide with a core route.
- **Keep dependencies to what's already installed** — the loader imports
  your file in the server's own environment. If you need a new package,
  add it to `requirements.txt` (and restart).
- **Plain code is fine** — a plugin can be a data table, a helper module,
  or a full feature with routes; there's no required structure beyond the
  three `PLUGIN_*` constants.
- **Test before trusting** — plugins run with the server's privileges.
  Develop in a copy of your `marketplace.db` if a plugin writes data.

## Enabling / disabling

- By default **all** `.py` files in `plugins/` load.
- To load only some, set `ENABLED_PLUGINS=Price Alerts,My Plugin` in
  `.env` (comma-separated `PLUGIN_NAME`s) and restart.

## The bundled example

`plugins/price_alerts.py` — tracks price changes on your listings. Read
it first; it's the reference implementation.
