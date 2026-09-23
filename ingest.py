"""
ingest.py
---------
Turns a user-uploaded data file into something query_agent.py and
connectors.py can already query -- WITHOUT changing connectors.py at all.

WHY THIS WORKS WITHOUT TOUCHING connectors.py:
SQLConnector (in connectors.py) is built on SQLAlchemy, and SQLAlchemy can
talk to many database engines through a single "connection_uri" string --
not just Postgres. SQLite is one of those engines, and it needs no server:
it's just a local file. So the plan is:

    uploaded file (.csv/.xlsx/.json/.parquet)
        -> read into a table
        -> written into a local SQLite file (e.g. "uploaded_data.db")
        -> return a dict shaped exactly like DATASET_CONFIG, pointing at it

From that point on, get_connector() builds a normal SQLConnector against
that SQLite file, and everything downstream (schema reading, run_query,
the guardrail, Gemini) behaves exactly as it already does for Postgres.

SUPPORTED FILE TYPES: .csv, .xlsx, .xls, .json, .parquet

HANDLING LARGE CSVs:
Reading a huge CSV fully into memory with pandas can be slow or crash on
very large files. For .csv specifically, this loads it in chunks (50,000
rows at a time) and appends each chunk into the SQLite table, so memory
usage stays roughly constant no matter how many rows the file has.
Excel/JSON/Parquet are loaded in one shot (chunked loading for those is
less standardized in pandas) -- fine for large files, but if you're
regularly ingesting huge Excel/JSON files, converting them to CSV or
Parquet first is the most memory-friendly option.
"""

import os
from sqlalchemy import create_engine


CHUNK_SIZE = 50_000   # rows per chunk when streaming large CSVs into SQLite


def load_file_to_dataset_config(
    file_path: str,
    table_name: str = "uploaded_data",
    db_path: str = "uploaded_data.db",
) -> dict:
    """Reads `file_path` and loads it into a local SQLite table, then returns
    a DATASET_CONFIG-shaped dict pointing at it. Raises ValueError for
    unsupported file types, and lets pandas/SQLAlchemy errors bubble up
    naturally for bad/corrupt files (caller should catch and show the user
    a clear message)."""

    import pandas as pd   # imported lazily so environments without pandas aren't affected until this is actually called

    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    engine = create_engine(f"sqlite:///{db_path}")

    if ext == ".csv":
        # Stream the file in chunks so a huge CSV doesn't need to fit in RAM
        # all at once.
        total_rows = 0
        columns = None
        first_chunk = True
        for chunk in pd.read_csv(file_path, chunksize=CHUNK_SIZE):
            chunk.to_sql(
                table_name,
                engine,
                if_exists="replace" if first_chunk else "append",
                index=False,
            )
            first_chunk = False
            total_rows += len(chunk)
            columns = list(chunk.columns)

        if columns is None:
            raise ValueError(f"'{file_path}' appears to be empty -- no rows were read.")

    elif ext in (".xlsx", ".xls"):
        df = pd.read_excel(file_path)
        df.to_sql(table_name, engine, if_exists="replace", index=False)
        total_rows, columns = len(df), list(df.columns)

    elif ext == ".json":
        df = pd.read_json(file_path)
        df.to_sql(table_name, engine, if_exists="replace", index=False)
        total_rows, columns = len(df), list(df.columns)

    elif ext == ".parquet":
        df = pd.read_parquet(file_path)
        df.to_sql(table_name, engine, if_exists="replace", index=False)
        total_rows, columns = len(df), list(df.columns)

    else:
        raise ValueError(
            f"Unsupported file type '{ext}'. Supported types: .csv, .xlsx, .xls, .json, .parquet"
        )

    return {
        "type": "sql",
        "connection_uri": f"sqlite:///{db_path}",
        "target": table_name,
        "description": (
            f"User-uploaded dataset from '{os.path.basename(file_path)}' "
            f"({total_rows:,} rows, {len(columns)} columns: {', '.join(columns[:8])}"
            f"{', ...' if len(columns) > 8 else ''})."
        ),
    }