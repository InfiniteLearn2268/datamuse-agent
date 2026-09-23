from dataset_config import DATASET_CONFIG
from connectors import get_connector

print("Creating connector...")

connector = get_connector(DATASET_CONFIG)

print("Connector created successfully!")
print("Query language:", connector.query_language)

print("\nDatabase schema:")
print(connector.get_schema())

print("\nRunning test query...")

rows = connector.run_query(
    "SELECT * FROM orders LIMIT 5"
)

print("\nQuery result:")
for row in rows:
    print(row)