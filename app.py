import os
import json
import logging
import io
import gzip
import zlib
from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash, check_password_hash

from flask import Flask, jsonify, request
from flask_cors import CORS
from flask import send_file

import boto3
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from botocore.config import Config 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

AWS_REGION = os.environ["AWS_REGION"]
DB_SECRET_ARN = os.environ["DB_SECRET_ARN"]
DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ["DB_NAME"]
DB_PORT = int(os.getenv("DB_PORT", "5432"))
APP_PORT = int(os.getenv("APP_PORT", "5000"))
S3_BUCKET_NAME = os.environ["S3_BUCKET_NAME"]
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "*")

app = Flask(__name__)

if ALLOWED_ORIGINS == "*":
    CORS(app, expose_headers=["Content-Disposition"])
else:
    CORS(
        app,
        resources={
            r"/*": {
                "origins": [origin.strip() for origin in ALLOWED_ORIGINS.split(",")],
                "expose_headers": ["Content-Disposition"]
            }
        }
    )

def get_db_secret():
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    response = client.get_secret_value(SecretId=DB_SECRET_ARN)
    return json.loads(response["SecretString"])

db_pool = None

def initialize_pool():
    global db_pool
    secret = get_db_secret()
    db_pool = SimpleConnectionPool(
        minconn=1,
        maxconn=20,
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=secret["username"],
        password=secret["password"]
    )
    logger.info("Database connection pool initialized")

def get_connection():
    return db_pool.getconn()

def release_connection(conn):
    db_pool.putconn(conn)

def create_tables_if_not_exists():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            # Users table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(100) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # Files table mapped to users
            cur.execute("""
                CREATE TABLE IF NOT EXISTS file_metadata (
                    id SERIAL PRIMARY KEY,
                    user_id INT REFERENCES users(id) ON DELETE CASCADE,
                    filename VARCHAR(255) NOT NULL,
                    s3_key VARCHAR(500) NOT NULL,
                    encryption_method VARCHAR(50) NOT NULL,
                    compression_method VARCHAR(50) NOT NULL,
                    file_size INT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.commit()
        logger.info("Database tables verified successfully.")
    finally:
        release_connection(conn)

def authenticate_user(username, password):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE username = %s;", (username,))
            user = cur.fetchone()
            if user and check_password_hash(user["password_hash"], password):
                return user
        return None
    finally:
        release_connection(conn)

def compress_data(data: bytes, method: str) -> bytes:
    if method == "gzip":
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=out, mode="wb") as f:
            f.write(data)
        return out.getvalue()
    elif method == "zlib":
        return zlib.compress(data)
    return data

def encrypt_data(data: bytes, method: str, key: str) -> bytes:
    if method == "fernet":
        f = Fernet(key.encode())
        return f.encrypt(data)
    return data

def decompress_data(data: bytes, method: str) -> bytes:
    if method == "gzip":
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as f:
                return f.read()
        except Exception:
            return data # fallback if not actually compressed
    elif method == "zlib":
        try:
            return zlib.decompress(data)
        except Exception:
            return data
    return data

def decrypt_data(data: bytes, method: str, key: str) -> bytes:
    if method == "fernet" and key:
        f = Fernet(key.encode())
        return f.decrypt(data)
    return data

# --- ROUTES ---

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "status": "healthy",
        "message": "API is running"
    }), 200


@app.route("/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    username = data.get("username")
    password = data.get("password")

    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400

    hashed_password = generate_password_hash(password)
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s) RETURNING id;",
                (username, hashed_password)
            )
            user_id = cur.fetchone()[0]
            conn.commit()
        return jsonify({"message": "User registered successfully", "user_id": user_id}), 201
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        return jsonify({"error": "Username already exists"}), 409
    finally:
        release_connection(conn)

@app.route("/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    user = authenticate_user(data.get("username"), data.get("password"))
    if not user:
        return jsonify({"error": "Invalid username or password"}), 401
    
    # For a simple PoC, return user_id directly so frontend can pass it along
    return jsonify({"message": "Login successful", "user_id": user["id"], "username": user["username"]}), 200

@app.route("/upload", methods=["POST"])
def upload_file():
    user_id = request.form.get("user_id")
    password = request.form.get("password") # Simple auth validation per request
    
    if not user_id or not password:
        return jsonify({"error": "user_id and password required for auth"}), 401
        
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM users WHERE id = %s;", (user_id,))
            user = cur.fetchone()
            if not user or not check_password_hash(user["password_hash"], password):
                return jsonify({"error": "Authentication failed"}), 401
    finally:
        release_connection(conn)

    if "file" not in request.files:
        return jsonify({"error": "No file part in request"}), 400
    
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    compression_method = request.form.get("compression_method", "none").lower()
    encryption_method = request.form.get("encryption_method", "none").lower()
    encryption_key = request.form.get("encryption_key", "")

    if encryption_method == "fernet" and not encryption_key:
        return jsonify({"error": "Encryption key required for fernet"}), 400

    try:
        file_bytes = file.read()
        compressed = compress_data(file_bytes, compression_method)
        processed = encrypt_data(compressed, encryption_method, encryption_key)

        s3_client = boto3.client(
            "s3", 
            region_name=AWS_REGION,
            config=Config(signature_version='s3v4') 
        )

        s3_key = f"user_{user_id}/{os.urandom(6).hex()}_{file.filename}.enc"

        s3_client.put_object(Bucket=S3_BUCKET_NAME, Key=s3_key, Body=processed)

        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    INSERT INTO file_metadata (user_id, filename, s3_key, encryption_method, compression_method, file_size)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, filename, s3_key, encryption_method, compression_method, file_size, created_at;
                    """,
                    (user_id, file.filename, s3_key, encryption_method, compression_method, len(processed))
                )
                row = cur.fetchone()
                conn.commit()
            return jsonify({"message": "Uploaded & processed successfully", "file": row}), 201
        finally:
            release_connection(conn)

    except Exception as e:
        logger.exception("Pipeline failed")
        return jsonify({"error": str(e)}), 500

@app.route("/files", methods=["POST"])
def list_user_files():
    data = request.get_json() or {}
    user = authenticate_user(data.get("username"), data.get("password"))
    if not user:
        return jsonify({"error": "Invalid credentials"}), 401

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT id, filename, s3_key, encryption_method, compression_method, file_size, created_at "
                "FROM file_metadata WHERE user_id = %s ORDER BY id DESC;",
                (user["id"],)
            )
            files = cur.fetchall()
        return jsonify(files), 200
    finally:
        release_connection(conn)

@app.route("/download-file", methods=["POST"])
def download_file():
    # Expecting JSON or Form data. Let's support JSON for credentials & preferences
    data = request.get_json() or {}
    username = data.get("username")
    password = data.get("password")
    file_id = data.get("file_id")
    
    # User choices for download (default to reversing them to give a readable file)
    want_decrypted = data.get("decrypt", True)
    encryption_key = data.get("encryption_key", "")
    want_decompressed = data.get("decompress", True)

    user = authenticate_user(username, password)
    if not user:
        return jsonify({"error": "Invalid credentials"}), 401

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM file_metadata WHERE id = %s AND user_id = %s;", (file_id, user["id"]))
            file_meta = cur.fetchone()
            if not file_meta:
                return jsonify({"error": "File not found or unauthorized"}), 404

        # Fetch file from S3
        s3_client = boto3.client("s3", region_name=AWS_REGION, config=Config(signature_version='s3v4'))
        s3_obj = s3_client.get_object(Bucket=S3_BUCKET_NAME, Key=file_meta['s3_key'])
        file_bytes = s3_obj['Body'].read()

        # 1. Reverse Encryption if requested
        if want_decrypted and file_meta['encryption_method'] != 'none':
            try:
                file_bytes = decrypt_data(file_bytes, file_meta['encryption_method'], encryption_key)
            except Exception as e:
                return jsonify({"error": f"Decryption failed (Check your key): {str(e)}"}), 400

        # 2. Reverse Compression if requested
        if want_decompressed and file_meta['compression_method'] != 'none':
            try:
                file_bytes = decompress_data(file_bytes, file_meta['compression_method'])
            except Exception as e:
                return jsonify({"error": f"Decompression failed: {str(e)}"}), 400

        # 3. Stream back with original filename
        return send_file(
            io.BytesIO(file_bytes),
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=file_meta['filename'] # Clean original filename like sample.txt!
        )

    except Exception as e:
        logger.exception("Download processing failed")
        return jsonify({"error": str(e)}), 500
    finally:
        release_connection(conn)

initialize_pool()
create_tables_if_not_exists()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=APP_PORT)