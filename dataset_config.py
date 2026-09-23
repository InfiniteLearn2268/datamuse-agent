"""
dataset_config.py
------------------
THIS FILE HOLDS THE ONE VARIABLE YOU CHANGE TO SWITCH BIG-DATA BACKENDS.

`DATASET_CONFIG` is a plain Python dict. Nothing else in the codebase needs to
change when you point it at a different kind of "big database" -- the
connector factory in connectors.py reads `DATASET_CONFIG["type"]` and builds
the right connector object.

Below are 4 ready-to-use examples (SQL, NoSQL, Vector/Search, Big-Data engine).
Uncomment the ONE you want active; keep the others as reference/templates.
"""

# ---------------------------------------------------------------------------
# EXAMPLE 1: Structured relational data (Postgres / MySQL / Snowflake, etc.)
# ---------------------------------------------------------------------------
DATASET_CONFIG = {
    "type": "sql",
    "connection_uri": "postgresql+psycopg2://postgres:Devang_DB%402026%23Run@localhost:5432/sales_db",
    "target": "orders",
    "description": "E-commerce orders, customers, and products tables.",
}

# ---------------------------------------------------------------------------
# EXAMPLE 2: Semi-structured / NoSQL document data (MongoDB, DynamoDB-style)
# ---------------------------------------------------------------------------
# DATASET_CONFIG = {
#     "type": "nosql",
#     "connection_uri": "mongodb://localhost:27017",
#     "database": "app_db",
#     "target": "user_events",                        # collection name
#     "description": "Raw clickstream / user event documents, nested JSON.",
# }

# ---------------------------------------------------------------------------
# EXAMPLE 3: Unstructured / vector & hybrid search (Elasticsearch/OpenSearch)
# ---------------------------------------------------------------------------
# DATASET_CONFIG = {
#     "type": "vector",
#     "connection_uri": "https://localhost:9200",
#     "target": "support_tickets",                    # index name
#     "embedding_field": "embedding",                 # dense_vector field for semantic search
#     "text_field": "body",                           # field with raw text, used for BM25/keyword
#     "description": "Customer support tickets, free text + resolution notes.",
# }

# ---------------------------------------------------------------------------
# EXAMPLE 4: Distributed / big-data engine (Spark SQL over Parquet/Delta lake)
# ---------------------------------------------------------------------------
# DATASET_CONFIG = {
#     "type": "bigdata",
#     "connection_uri": "local[*]",                    # Spark master; e.g. "spark://cluster:7077" in prod
#     "target": "s3a://my-lake/events_parquet/",       # path Spark registers as a temp view
#     "view_name": "events",                            # SQL name the agent will query
#     "description": "Petabyte-scale event log stored as partitioned Parquet.",
# }
