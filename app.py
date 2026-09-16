import os
import json
import logging

from flask import Flask, jsonify, request
from flask_cors import CORS

import boto3
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor

# --------------------------------------------------
# Logging
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(__name__)

# --------------------------------------------------
# Environment Variables
# --------------------------------------------------

AWS_REGION = os.environ["AWS_REGION"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
APP_PORT = int(os.getenv("APP_PORT", "5000"))

ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "*"
)

# --------------------------------------------------
# Flask
# --------------------------------------------------

app = Flask(__name__)

CORS(
    app,
    resources={
        r"/*": {
            "origins": ALLOWED_ORIGINS.split(",")
            if ALLOWED_ORIGINS != "*"
            else "*"
        }
    }
)

# --------------------------------------------------
# Secrets Manager
# --------------------------------------------------

def get_db_secret():
    client = boto3.client(
        "secretsmanager",
        region_name=AWS_REGION
    )

    response = client.get_secret_value(
        SecretId=DB_SECRET_ARN
    )

    return json.loads(response["SecretString"])


# --------------------------------------------------
# Database Pool
# --------------------------------------------------

db_pool = None

def initialize_pool():
    global db_pool

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

    logger.info("Database connection pool established")


def get_connection():
    return db_pool.getconn()


def release_connection(conn):
    db_pool.putconn(conn)


# --------------------------------------------------
# Table Creation
# --------------------------------------------------

def create_table_if_not_exists():
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS test_items (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    description TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            conn.commit()

        logger.info("test_items table verified")

    finally:
        release_connection(conn)


# --------------------------------------------------
# Health Endpoint
# --------------------------------------------------

@app.route("/health", methods=["GET"])
def health():

    return jsonify({
        "status": "healthy",
        "service": "postgres-api"
    }), 200


# --------------------------------------------------
# DB Health Endpoint
# --------------------------------------------------

@app.route("/db-health", methods=["GET"])
def db_health():

    conn = None

    try:
        conn = get_connection()

        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            result = cur.fetchone()

        return jsonify({
            "database": "healthy",
            "result": result[0]
        }), 200

    except Exception as e:

        return jsonify({
            "database": "unhealthy",
            "error": str(e)
        }), 500

    finally:
        if conn:
            release_connection(conn)


# --------------------------------------------------
# CREATE
# --------------------------------------------------

@app.route("/items", methods=["POST"])
def create_item():

    data = request.get_json()

    if not data:
        return jsonify({"error": "Request body required"}), 400

    name = data.get("name")
    description = data.get("description")

    if not name:
        return jsonify({"error": "name is required"}), 400

    conn = get_connection()

    try:

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute(
                """
                INSERT INTO test_items(name, description)
                VALUES (%s, %s)
                RETURNING *;
                """,
                (name, description)
            )

            item = cur.fetchone()
            conn.commit()

        return jsonify(item), 201

    finally:
        release_connection(conn)


# --------------------------------------------------
# READ ALL
# --------------------------------------------------

@app.route("/items", methods=["GET"])
def get_items():

    conn = get_connection()

    try:

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute(
                """
                SELECT *
                FROM test_items
                ORDER BY id;
                """
            )

            rows = cur.fetchall()

        return jsonify(rows), 200

    finally:
        release_connection(conn)


# --------------------------------------------------
# READ ONE
# --------------------------------------------------

@app.route("/items/<int:item_id>", methods=["GET"])
def get_item(item_id):

    conn = get_connection()

    try:

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute(
                """
                SELECT *
                FROM test_items
                WHERE id = %s;
                """,
                (item_id,)
            )

            row = cur.fetchone()

        if not row:
            return jsonify({
                "error": "Item not found"
            }), 404

        return jsonify(row), 200

    finally:
        release_connection(conn)


# --------------------------------------------------
# UPDATE
# --------------------------------------------------

@app.route("/items/<int:item_id>", methods=["PUT"])
def update_item(item_id):

    data = request.get_json()

    name = data.get("name")
    description = data.get("description")

    conn = get_connection()

    try:

        with conn.cursor(cursor_factory=RealDictCursor) as cur:

            cur.execute(
                """
                UPDATE test_items
                SET
                    name = %s,
                    description = %s
                WHERE id = %s
                RETURNING *;
                """,
                (
                    name,
                    description,
                    item_id
                )
            )

            row = cur.fetchone()
            conn.commit()

        if not row:
            return jsonify({
                "error": "Item not found"
            }), 404

        return jsonify(row), 200

    finally:
        release_connection(conn)


# --------------------------------------------------
# DELETE
# --------------------------------------------------

@app.route("/items/<int:item_id>", methods=["DELETE"])
def delete_item(item_id):

    conn = get_connection()

    try:

        with conn.cursor() as cur:

            cur.execute(
                """
                DELETE FROM test_items
                WHERE id = %s;
                """,
                (item_id,)
            )

            deleted = cur.rowcount

            conn.commit()

        if deleted == 0:
            return jsonify({
                "error": "Item not found"
            }), 404

        return jsonify({
            "message": "Deleted successfully"
        }), 200

    finally:
        release_connection(conn)


# --------------------------------------------------
# Startup
# --------------------------------------------------

initialize_pool()
create_table_if_not_exists()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=APP_PORT
    )