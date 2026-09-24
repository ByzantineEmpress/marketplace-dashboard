"""Marketplace Dashboard - Database setup using SQLAlchemy."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from src.config import config

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

    # 2. A default team that everything (and the local admin) belongs to
    import secrets
    db = SessionLocal()
    try:
        default_team = db.query(Team).filter(Team.name == "Default Team").first()
        if default_team is None:
            default_team = Team(name="Default Team", invite_code=secrets.token_urlsafe(16))
            db.add(default_team)
            db.flush()

            # The local username/password login becomes a real user + owner
            admin = db.query(User).filter(User.provider == "local").first()
            if admin is None:
                admin = User(email=config.ADMIN_USERNAME + "@local", name="Admin", provider="local", is_admin=True)
                db.add(admin)
                db.flush()
            else:
                admin.is_admin = True
            db.add(TeamMembership(team_id=default_team.id, user_id=admin.id, role="owner"))
        else:
            local_user = db.query(User).filter(User.provider == "local").first()
            if local_user and not local_user.is_admin:
                local_user.is_admin = True

        # Ensure all existing teams have an invite code
        for t in db.query(Team).filter(Team.invite_code == None).all():  # noqa: E711
            t.invite_code = secrets.token_urlsafe(16)

        # 3. Unassigned listings go to the default team (shared)
        db.query(Listing).filter(Listing.team_id == None).update(  # noqa: E711
            {Listing.team_id: default_team.id},
            synchronize_session=False,
        )
        db.commit()
    finally:
        db.close()
