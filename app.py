import os
import json
import logging

from flask import Flask, jsonify, request
from flask_cors import CORS

import boto3
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

AWS_REGION = os.environ["AWS_REGION"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
APP_PORT = int(os.getenv("APP_PORT", "5000"))
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")

app = Flask(__name__)

CORS(
    app,
    resources={
        r"/*": {
            "origins": ALLOWED_ORIGINS.split(",") if ALLOWED_ORIGINS != "*" else "*"
        }
    }
)

# Global pool pointer
db_pool = None

def get_db_secret():
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    response = client.get_secret_value(SecretId=DB_SECRET_ARN)
    return json.loads(response["SecretString"])

def get_connection():
    global db_pool
    # Lazy initialization happens inside the running worker process, NOT at script boot
    if db_pool is None:
        logger.info("Initializing database connection pool for this worker process...")
        secret = get_db_secret()
        db_pool = SimpleConnectionPool(
            minconn=1,
            maxconn=20,
            host=secret["host"],
            port=secret["port"],
            database=secret["dbname"],
            user=secret["username"],
            password=secret["password"]
        )
    return db_pool.getconn()

def release_connection(conn):
    if db_pool:
        db_pool.putconn(conn)

# Safely handle table creation inside an app context setup
@app.before_all_requests
def setup_database_schema():
    """Runs once per worker process before handling its first request."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS test_items (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
        logger.info("Database schema verified safely within worker process context.")
    except Exception as e:
        conn.rollback() # Crucial rollback
        logger.error(f"Failed to verify schema: {e}")
        raise e
    finally:
        release_connection(conn)

# Example of a fully fixed CRUD endpoint with Error Handling and Rollbacks
@app.route("/items", methods=["POST"])
def create_item():
    data = request.get_json()
    if not data or not data.get("name"):
        return jsonify({"error": "name is required"}), 400

    name = data.get("name")
    description = data.get("description")
    
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO test_items(name, description) VALUES (%s, %s) RETURNING *;",
                (name, description)
            )
            item = cur.fetchone()
            conn.commit() # Commit only on clean success
        return jsonify(item), 201
    except Exception as e:
        conn.rollback() # CRITICAL: If SQL fails, rollback to save the connection state
        logger.error(f"Database write error: {e}")
        return jsonify({"error": "Internal database error"}), 500
    finally:
        release_connection(conn)

# Keep the bottom clean for Gunicorn compatibility
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT)
