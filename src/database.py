"""Marketplace Dashboard - Database setup using SQLAlchemy."""

import secrets

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from src.config import config

# Invite codes are 16 CSPRNG bytes (128 bits) rendered as 22 URL-safe chars.
INVITE_CODE_BYTES = 16


def new_invite_code() -> str:
    """Return a fresh team invite code with full cryptographic entropy.

    16 bytes from ``secrets`` (a CSPRNG) = 128 bits, rendered as 22 URL-safe
    characters. Even at a million guesses per second the expected search is
    ~10^22 years, so the code alone is a sufficient bearer credential for
    joining a team. Every invite-code creation path uses this function so the
    strength cannot silently regress in one place but not another.
    """
    return secrets.token_urlsafe(INVITE_CODE_BYTES)

# Create engine based on database URL
engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False} if "sqlite" in config.DATABASE_URL else {})

# SQLite only: enable WAL mode + a busy timeout.
# WAL lets readers and writers run concurrently, so the browser (a
# separate "process" from the server) always sees freshly committed
# data instead of a stale snapshot.
if "sqlite" in config.DATABASE_URL:
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


def get_db():
    """Yield a database session for each request.

    Ensures the session is closed after the request completes,
    preventing connection leaks.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all tables defined by SQLAlchemy models.

    Safe to call multiple times — tables are only created if they don't exist.
    Also runs lightweight migrations for databases that predate new features.
    """
    import src.models  # noqa: F401  # Import to register all models with Base
    Base.metadata.create_all(bind=engine)
    _migrate_existing_db()


def _migrate_existing_db():
    """Lightweight one-off migrations for databases created before Teams.

    SQLite's create_all() creates missing *tables* but never adds missing
    *columns* to existing tables, so an old marketplace.db would lack
    ``listings.team_id``. Every step is idempotent and cheap — running it
    on every startup is fine.
    """
    from sqlalchemy import text
    from src import config
    from src.models import Listing, Team, User, TeamMembership

    # 1. listings.team_id column (missing on old DBs)
    with engine.begin() as conn:
        # 1. listings.team_id column (missing on old DBs)
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(listings)"))}
        if "team_id" not in cols:
            conn.execute(text("ALTER TABLE listings ADD COLUMN team_id INTEGER REFERENCES teams(id)"))

        # 1b. teams.invite_code column (for shareable invite links)
        team_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(teams)"))}
        if "invite_code" not in team_cols:
            conn.execute(text("ALTER TABLE teams ADD COLUMN invite_code VARCHAR(32)"))

        # 1c. Cost tracking columns (COGS & Parts)
        if "purchase_price_cents" not in cols:
            conn.execute(text("ALTER TABLE listings ADD COLUMN purchase_price_cents INTEGER NOT NULL DEFAULT 0"))
        if "parts_cost_cents" not in cols:
            conn.execute(text("ALTER TABLE listings ADD COLUMN parts_cost_cents INTEGER NOT NULL DEFAULT 0"))
        if "parts_json" not in cols:
            conn.execute(text("ALTER TABLE listings ADD COLUMN parts_json JSON DEFAULT '[]'"))

        # 1d. Multi-platform cross-listing
        if "platforms_json" not in cols:
            conn.execute(text("ALTER TABLE listings ADD COLUMN platforms_json JSON DEFAULT '[]'"))

        # 1e. sold_at timestamp for date-range metrics
        if "sold_at" not in cols:
            conn.execute(text("ALTER TABLE listings ADD COLUMN sold_at TIMESTAMP"))

        # 1f. written_off_at timestamp for write-off tracking
        if "written_off_at" not in cols:
            conn.execute(text("ALTER TABLE listings ADD COLUMN written_off_at TIMESTAMP"))

        # 1g. users.is_admin column for role-based access control
        user_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(users)"))}
        if "is_admin" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))

        # 1h. teams.created_by_email so a prospective member can ask to join by
        #     naming the creator's email address.
        if "created_by_email" not in team_cols:
            conn.execute(text("ALTER TABLE teams ADD COLUMN created_by_email VARCHAR(255)"))

        # 1i. users.password_hash for self-serve email+password accounts.
        if "password_hash" not in user_cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(255)"))

    # 1j. marketplace_accounts must be per-user.
    #
    # The original schema had UNIQUE(platform), which allowed exactly ONE eBay
    # connection for the whole instance, and no user_id at all — so every
    # adapter query was global and two users would overwrite each other's
    # tokens. SQLite cannot drop a UNIQUE constraint in place, so the table is
    # rebuilt: create_all() is invoked again after DROP so the new shape comes
    # from the model rather than hand-written DDL that could drift from it.
    _rebuild_marketplace_accounts_for_users()

    # 2. A default team that everything (and the local admin) belongs to
    import secrets
    db = SessionLocal()
    try:
        default_team = db.query(Team).filter(Team.name == "Default Team").first()
        if default_team is None:
            default_team = Team(name="Default Team", invite_code=new_invite_code(),
                                created_by_email=f"{config.ADMIN_USERNAME}@local".lower())
            db.add(default_team)
            db.flush()

        # Identify the local administrator by *both* provider and username.
        # Matching on provider alone would treat any future local-provider
        # account as the admin and silently hand it the shared team.
        admin_email = f"{config.ADMIN_USERNAME}@local"
        admin = (
            db.query(User)
            .filter(User.provider == "local", User.email == admin_email)
            .first()
        )
        if admin is None:
            admin = db.query(User).filter(User.provider == "local").first()
        if admin is None:
            admin = User(email=admin_email, name="Admin", provider="local", is_admin=True)
            db.add(admin)
            db.flush()
        else:
            admin.is_admin = True

        # The Default Team is the local admin's own workspace: it must contain
        # the admin and nobody else. Any other member there would share the
        # admin's inventory with them, which is exactly what tenant isolation
        # is meant to prevent.
        db.query(TeamMembership).filter(
            TeamMembership.team_id == default_team.id,
            TeamMembership.user_id != admin.id,
        ).delete(synchronize_session=False)

        existing_admin_membership = (
            db.query(TeamMembership)
            .filter(
                TeamMembership.team_id == default_team.id,
                TeamMembership.user_id == admin.id,
            )
            .first()
        )
        if existing_admin_membership is None:
            db.add(TeamMembership(team_id=default_team.id, user_id=admin.id, role="owner"))
        elif existing_admin_membership.role != "owner":
            existing_admin_membership.role = "owner"

        # 2b. Back-fill teams.created_by_email from their current owner, so
        #     teams that predate this column can still be asked to join by
        #     naming the creator's address. Prefer an owner-role membership,
        #     fall back to any member so a team is never unmatchable.
        unowned = db.query(Team).filter(
            (Team.created_by_email == None) | (Team.created_by_email == "")  # noqa: E711
        ).all()
        for team in unowned:
            owner_row = (
                db.query(TeamMembership)
                .filter(
                    TeamMembership.team_id == team.id,
                    TeamMembership.role == "owner",
                )
                .order_by(TeamMembership.id)
                .first()
            ) or (
                db.query(TeamMembership)
                .filter(TeamMembership.team_id == team.id)
                .order_by(TeamMembership.id)
                .first()
            )
            if owner_row is None:
                continue
            owner_user = db.query(User).filter(User.id == owner_row.user_id).first()
            if owner_user and owner_user.email:
                team.created_by_email = owner_user.email

        # Ensure all existing teams have an invite code
        for t in db.query(Team).filter(Team.invite_code == None).all():  # noqa: E711
            t.invite_code = new_invite_code()

        # 3. Unassigned listings go to the default team (shared)
        db.query(Listing).filter(Listing.team_id == None).update(  # noqa: E711
            {Listing.team_id: default_team.id},
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()


def _rebuild_marketplace_accounts_for_users():
    """Convert marketplace_accounts to a per-user table.

    Before this, ``platform`` was UNIQUE and there was no ``user_id``, so the
    instance could hold exactly ONE eBay connection, shared by everybody, and
    every adapter query was global. SQLite cannot remove a UNIQUE constraint
    with ALTER TABLE, so the table must be rebuilt.

    The rebuild is driven by ``Base.metadata.create_all`` rather than hand
    written DDL, so the resulting schema cannot drift from the model.

    Any existing rows are preserved and adopted by the local administrator:
    they predate per-user ownership, and there is no other honest way to decide
    who they belonged to. The table is empty on every deployment so far, but the
    copy is written anyway so the step is safe in general.

    Idempotent: returns immediately once ``user_id`` is present.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        exists = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='marketplace_accounts'"
            )
        ).fetchone()
        if not exists:
            return

        cols = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(marketplace_accounts)"))
        }
        if "user_id" in cols:
            return  # already migrated

        rows = [
            dict(r._mapping)
            for r in conn.execute(text("SELECT * FROM marketplace_accounts"))
        ]
        conn.execute(text("DROP TABLE marketplace_accounts"))

    # Recreate from the model, so the shape comes from src/models.py.
    Base.metadata.create_all(bind=engine)

    if not rows:
        return

    with engine.begin() as conn:
        owner = conn.execute(
            text("SELECT id FROM users WHERE provider='local' ORDER BY id LIMIT 1")
        ).fetchone()
        owner_id = owner[0] if owner else None
        for row in rows:
            conn.execute(
                text(
                    "INSERT INTO marketplace_accounts "
                    "(user_id, platform, shop_id, shop_name, access_token, "
                    " refresh_token, token_expires_at, token_data, is_connected, "
                    " last_synced) "
                    "VALUES (:user_id, :platform, :shop_id, :shop_name, "
                    " :access_token, :refresh_token, :token_expires_at, "
                    " :token_data, :is_connected, :last_synced)"
                ),
                {
                    "user_id": owner_id,
                    "platform": row.get("platform"),
                    "shop_id": row.get("shop_id"),
                    "shop_name": row.get("shop_name"),
                    # Values written before EncryptedText existed are plaintext;
                    # re-inserting them this way leaves them readable, and they
                    # are encrypted the next time the row is written.
                    "access_token": row.get("access_token"),
                    "refresh_token": row.get("refresh_token"),
                    "token_expires_at": row.get("token_expires_at"),
                    "token_data": row.get("token_data"),
                    "is_connected": row.get("is_connected", 0),
                    "last_synced": row.get("last_synced"),
                },
            )
        print(
            f"[migration] marketplace_accounts rebuilt for per-user ownership; "
            f"{len(rows)} existing row(s) assigned to user_id={owner_id}"
        )
