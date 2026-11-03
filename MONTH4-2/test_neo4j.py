import os
from dotenv import load_dotenv
from neo4j import GraphDatabase

load_dotenv()

uri = os.getenv("NEO4J_URI")
user = os.getenv("NEO4J_USERNAME")
password = os.getenv("NEO4J_PASSWORD")

print(f"Connecting to: {uri}")
try:
    driver = GraphDatabase.driver(uri, auth=(user, password), connection_timeout=5.0)
    driver.verify_connectivity()
    print("SUCCESS: Connected to Neo4j!")
    driver.close()
except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}")