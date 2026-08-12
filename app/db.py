"""SQLite engine, session helper, and settings key-value accessors."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select

from .config import config
from .models import Setting

log = logging.getLogger(__name__)

# check_same_thread=False: APScheduler jobs and request handlers both touch the DB.
engine = create_engine(
    config.db_url,
    echo=False,
    connect_args={"check_same_thread": False},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record):
    """WAL keeps the scheduler writing while the dashboard reads."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()
    _relax_nullability()
    _repair_enum_defaults()
    with Session(engine) as session:
        _seed_defaults(session)


def _add_missing_columns() -> None:
    """Tiny forward-only migration.

    create_all() creates missing tables but never alters existing ones, so a
    new model field would silently break an app.db that already has rows.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = f"{column.name} {column.type.compile(engine.dialect)}"
                literal = _default_literal(column)
                if literal is not None:
                    ddl += f" DEFAULT {literal}"
                conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
                log.info("Migrated: added %s.%s", table.name, column.name)


def _default_literal(column) -> str | None:
    """SQL literal for a column default, or None if it has no usable one.

    Enum members need `.value`: str(SomeEnum.member) renders as
    'SomeEnum.member' on Python 3.12, which would be written into every
    existing row and then fail to load back as a valid enum.
    """
    if column.default is None:
        return None
    value = getattr(column.default, "arg", None)
    if callable(value) or value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return None


def _relax_nullability() -> None:
    """Rebuild tables whose columns became optional in the model.

    SQLite cannot drop a NOT NULL constraint with ALTER TABLE, so a column
    that was required when the table was first created stays required
    forever. `schedules.target_id` hit exactly this: it became optional when
    campaigns arrived, and campaign-only rows could not insert.

    The fix is the documented SQLite dance - create the table fresh under a
    temporary name, copy the rows across, drop the old one, rename.
    """
    from sqlalchemy import inspect, text
    from sqlalchemy.schema import CreateTable

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())

    for table in SQLModel.metadata.sorted_tables:
        if table.name not in existing:
            continue

        db_columns = {c["name"]: c for c in inspector.get_columns(table.name)}
        needs_rebuild = [
            col.name
            for col in table.columns
            if col.nullable
            and col.name in db_columns
            and not db_columns[col.name]["nullable"]
            and not col.primary_key
        ]
        if not needs_rebuild:
            continue

        log.info(
            "Rebuilding %s to make %s optional",
            table.name, ", ".join(needs_rebuild),
        )

        shared = [c.name for c in table.columns if c.name in db_columns]
        columns_sql = ", ".join(f'"{name}"' for name in shared)
        temp_name = f"{table.name}__rebuild"

        # Build the replacement inside the real metadata so its foreign keys
        # can resolve the tables they point at, then remove it again so a later
        # create_all() does not resurrect the temporary name.
        fresh = table.to_metadata(SQLModel.metadata, name=temp_name)
        # Indexes are recreated by create_all afterwards; carrying them here
        # would clash with the originals' names.
        fresh.indexes.clear()

        try:
            # PRAGMA foreign_keys cannot change inside a transaction.
            with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                try:
                    conn.execute(text(f'DROP TABLE IF EXISTS "{temp_name}"'))
                    conn.execute(CreateTable(fresh))
                    conn.execute(
                        text(
                            f'INSERT INTO "{temp_name}" ({columns_sql}) '
                            f'SELECT {columns_sql} FROM "{table.name}"'
                        )
                    )
                    conn.execute(text(f'DROP TABLE "{table.name}"'))
                    conn.execute(
                        text(f'ALTER TABLE "{temp_name}" RENAME TO "{table.name}"')
                    )
                finally:
                    conn.execute(text("PRAGMA foreign_keys=ON"))
        finally:
            SQLModel.metadata.remove(fresh)

    # Recreate any indexes the rebuilt tables lost.
    SQLModel.metadata.create_all(engine)


def _repair_enum_defaults() -> None:
    """Undo rows written by the earlier buggy default (e.g. 'OutputType.message')."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if table.name not in tables:
                continue
            columns = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name not in columns or column.default is None:
                    continue
                value = getattr(column.default, "arg", None)
                if not isinstance(value, Enum):
                    continue
                broken = f"{type(value).__name__}.{value.name}"
                result = conn.execute(
                    text(
                        f"UPDATE {table.name} SET {column.name} = :good "
                        f"WHERE {column.name} = :bad"
                    ),
                    {"good": value.value, "bad": broken},
                )
                if result.rowcount:
                    log.info(
                        "Repaired %s row(s) in %s.%s (%s -> %s)",
                        result.rowcount, table.name, column.name, broken, value.value,
                    )


def _seed_defaults(session: Session) -> None:
    defaults = {
        "default_model": config.default_model,
        "global_sending_enabled": "true",
    }
    changed = False
    for key, value in defaults.items():
        if session.get(Setting, key) is None:
            session.add(Setting(key=key, value=value))
            changed = True
    if changed:
        session.commit()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(engine) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    """For background jobs, which have no request to hang a dependency on."""
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# settings helpers
# ---------------------------------------------------------------------------


def get_setting(session: Session, key: str, default: str = "") -> str:
    row = session.get(Setting, key)
    return row.value if row else default


def set_setting(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
        session.add(row)
    session.commit()


def all_settings(session: Session) -> dict[str, str]:
    return {row.key: row.value for row in session.exec(select(Setting)).all()}
