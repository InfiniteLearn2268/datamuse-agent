"""
connectors.py
-------------
Defines ONE common interface (`BaseConnector`) that every backend implements,
so the LLM agent never has to know whether it's talking to Postgres, MongoDB,
Elasticsearch, or Spark. Each connector exposes exactly three things:

    .query_language   -> str, tells the LLM what dialect to write ("sql", "mongo", etc.)
    .get_schema()      -> str, a text description of fields/tables the LLM can read
    .run_query(query)  -> list[dict], the executed results

A factory function `get_connector(config)` builds the right one from
`DATASET_CONFIG`.
"""

from abc import ABC, abstractmethod          # ABC = Abstract Base Class, enforces a shared interface
from typing import Any


# ---------------------------------------------------------------------------
# Base interface
# ---------------------------------------------------------------------------
class BaseConnector(ABC):
    """Every backend-specific connector must implement these 3 methods."""

    query_language: str = "unknown"          # overridden by each subclass, e.g. "sql", "mongo-json"

    @abstractmethod
    def get_schema(self) -> str:
        """Return a human/LLM-readable description of available fields/tables."""
        raise NotImplementedError

    @abstractmethod
    def run_query(self, query: Any) -> list[dict]:
        """Execute the query and return rows/documents as a list of dicts."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. SQL connector (Postgres / MySQL / Snowflake / BigQuery via SQLAlchemy)
# ---------------------------------------------------------------------------
class SQLConnector(BaseConnector):
    query_language = "sql"

    def __init__(self, config: dict):
        from sqlalchemy import create_engine, inspect   # imported lazily so other backends don't need this dep

        self.engine = create_engine(config["connection_uri"])   # opens a pooled DB connection
        self.target_table = config["target"]                    # e.g. "orders"
        self.inspector = inspect(self.engine)                   # SQLAlchemy's schema-reading helper

    def get_schema(self) -> str:
        # Pull real column names + types straight from the live database ("schema linking" / grounding),
        # so the LLM writes SQL against columns that actually exist instead of guessing.
        columns = self.inspector.get_columns(self.target_table)
        lines = [f"Table: {self.target_table}"]
        for col in columns:
            lines.append(f"  - {col['name']} ({col['type']})")
        return "\n".join(lines)

    def run_query(self, query: str) -> list[dict]:
        from sqlalchemy import text
        with self.engine.connect() as conn:              # opens+closes a connection safely (context manager)
            result = conn.execute(text(query))            # executes the raw SQL string the LLM generated
            columns = result.keys()                       # column names of the result set
            return [dict(zip(columns, row)) for row in result.fetchall()]  # rows -> list[dict]


# ---------------------------------------------------------------------------
# 2. NoSQL connector (MongoDB-style document store)
# ---------------------------------------------------------------------------
class NoSQLConnector(BaseConnector):
    query_language = "mongo-json"          # the LLM will emit a JSON filter dict, not SQL text

    def __init__(self, config: dict):
        from pymongo import MongoClient

        client = MongoClient(config["connection_uri"])   # connect to the Mongo cluster/instance
        self.db = client[config["database"]]              # select the logical database
        self.collection = self.db[config["target"]]       # select the collection (like a "table")

    def get_schema(self) -> str:
        # Mongo has no fixed schema, so we infer field names by sampling a few real documents.
        sample_docs = list(self.collection.find().limit(3))
        fields = sorted({key for doc in sample_docs for key in doc.keys()})
        return f"Collection: {self.collection.name}\nObserved fields: {fields}\nSample doc: {sample_docs[0] if sample_docs else '{}'}"

    def run_query(self, query: dict) -> list[dict]:
        # `query` is expected to be a Mongo filter dict, e.g. {"status": "shipped"}
        cursor = self.collection.find(query).limit(200)   # cap results so the agent/LLM isn't flooded
        return [{k: v for k, v in doc.items() if k != "_id"} for doc in cursor]


# ---------------------------------------------------------------------------
# 3. Vector / hybrid search connector (Elasticsearch / OpenSearch style)
# ---------------------------------------------------------------------------
class VectorConnector(BaseConnector):
    query_language = "es-dsl"              # Elasticsearch Query DSL (JSON), possibly with a knn clause

    def __init__(self, config: dict):
        from elasticsearch import Elasticsearch

        self.client = Elasticsearch(config["connection_uri"])   # connect to the ES/OpenSearch cluster
        self.index = config["target"]                            # index name to search
        self.text_field = config["text_field"]                    # field used for keyword/BM25 matching
        self.embedding_field = config["embedding_field"]           # field used for vector/knn matching

    def get_schema(self) -> str:
        mapping = self.client.indices.get_mapping(index=self.index)   # ES's schema = "mapping"
        fields = list(mapping[self.index]["mappings"]["properties"].keys())
        return f"Index: {self.index}\nFields: {fields}\nText field: {self.text_field}\nVector field: {self.embedding_field}"

    def run_query(self, query: dict) -> list[dict]:
        # `query` is expected to be a full Elasticsearch DSL body, e.g. {"query": {"match": {...}}}
        response = self.client.search(index=self.index, body=query, size=20)
        return [hit["_source"] for hit in response["hits"]["hits"]]   # unwrap the matched documents


# ---------------------------------------------------------------------------
# 4. Big-data connector (Spark SQL over a lake of Parquet/Delta files)
# ---------------------------------------------------------------------------
class BigDataConnector(BaseConnector):
    query_language = "spark-sql"

    def __init__(self, config: dict):
        from pyspark.sql import SparkSession

        self.spark = (
            SparkSession.builder
            .master(config["connection_uri"])          # e.g. "local[*]" or "spark://cluster:7077"
            .appName("agentic-retrieval")
            .getOrCreate()
        )
        self.view_name = config["view_name"]             # SQL alias the agent will query
        df = self.spark.read.parquet(config["target"])   # lazily reads the big dataset (no full load yet)
        df.createOrReplaceTempView(self.view_name)         # registers it so Spark SQL can query it by name

    def get_schema(self) -> str:
        df = self.spark.table(self.view_name)
        lines = [f"View: {self.view_name}"]
        for field in df.schema.fields:                    # Spark's schema objects, like SQLAlchemy's columns
            lines.append(f"  - {field.name} ({field.dataType})")
        return "\n".join(lines)

    def run_query(self, query: str) -> list[dict]:
        result_df = self.spark.sql(query)                 # runs Spark SQL, distributed under the hood
        return [row.asDict() for row in result_df.limit(200).collect()]   # cap + pull to driver as dicts


# ---------------------------------------------------------------------------
# Factory: builds the right connector from DATASET_CONFIG["type"]
# ---------------------------------------------------------------------------
_CONNECTOR_REGISTRY = {
    "sql": SQLConnector,
    "nosql": NoSQLConnector,
    "vector": VectorConnector,
    "bigdata": BigDataConnector,
}


def get_connector(config: dict) -> BaseConnector:
    """Single entry point the rest of the app uses -- reads config['type'] and
    instantiates the matching connector class. This is the only place that
    needs to know all four backend types exist."""
    backend_type = config["type"]
    connector_cls = _CONNECTOR_REGISTRY.get(backend_type)
    if connector_cls is None:
        raise ValueError(f"Unknown dataset type '{backend_type}'. Valid options: {list(_CONNECTOR_REGISTRY)}")
    return connector_cls(config)
