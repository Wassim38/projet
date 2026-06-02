import os
import json
import logging
from datetime import datetime
import psycopg2
from urllib.parse import parse_qs

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

class DateTimeAndUUIDEncoder(json.JSONEncoder):
    """
    Custom JSON encoder to properly serialize datetimes and UUIDs.
    """
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, 'hex'):  # UUIDs
            return str(obj)
        return super(DateTimeAndUUIDEncoder, self).default(obj)

def read_secret(secret_name, key, default=None):
    """
    Reads a secret value mounted by OpenFaaS inside the container.
    Supports both flat mount (/var/openfaas/secrets/key) and 
    structured mount (/var/openfaas/secrets/secret_name/key).
    Falls back to environment variables.
    """
    path_flat = f"/var/openfaas/secrets/{key}"
    path_structured = f"/var/openfaas/secrets/{secret_name}/{key}"
    
    for path in [path_flat, path_structured]:
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return f.read().strip()
            except Exception as e:
                logger.error(f"Error reading secret file {path}: {e}")
                
    # Fallback to env var format (e.g. DB_CREDENTIALS_HOST)
    env_name = f"{secret_name.upper().replace('-', '_')}_{key.upper()}"
    return os.environ.get(env_name, default)

def get_db_connection():
    """
    Establishes and returns a connection to the PostgreSQL database.
    """
    host = read_secret("db-credentials", "host", "host.docker.internal")
    port = read_secret("db-credentials", "port", "5432")
    user = read_secret("db-credentials", "user", "postgres")
    password = read_secret("db-credentials", "password", "postgres-password")
    dbname = read_secret("db-credentials", "dbname", "saga_ecommerce")

    return psycopg2.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=dbname,
        connect_timeout=5
    )

def handle(event, context):
    """
    OpenFaaS synchronous handler logic.
    Exposes GET API to list order history: /?user_id=X or /?status=Y
    """
    logger.info("Received request in order-history-api")
    
    # 1. Parse Query Parameters
    # Standardize event.query to a dict. OpenFaaS templates vary depending on local version.
    query_params = {}
    
    # Check event.query
    if hasattr(event, 'query') and event.query:
        if isinstance(event.query, dict):
            query_params = event.query
        else:
            # If string representation, parse it
            query_params = {k: v[0] for k, v in parse_qs(str(event.query)).items()}
    # Fallback to query_string
    elif hasattr(event, 'query_string') and event.query_string:
        query_params = {k: v[0] for k, v in parse_qs(event.query_string).items()}
    # Fallback to event as a dict
    elif isinstance(event, dict) and event.get("query"):
        if isinstance(event["query"], dict):
            query_params = event["query"]
        else:
            query_params = {k: v[0] for k, v in parse_qs(str(event["query"])).items()}

    user_id = query_params.get("user_id")
    status = query_params.get("status")

    logger.info(f"Filtering order history by: user_id={user_id}, status={status}")

    # 2. Build PostgreSQL query dynamically (using parameterized queries to prevent SQL injections)
    conn = None
    orders_list = []
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            query = """
                SELECT order_id, user_id, cart_id, total_amount, status, invoice_url, created_at, updated_at
                FROM orders
            """
            conditions = []
            params = []

            if user_id:
                conditions.append("user_id = %s")
                params.append(user_id)
            
            if status:
                conditions.append("status = %s")
                params.append(status)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            
            # Order by latest created orders
            query += " ORDER BY created_at DESC LIMIT 100;"

            cur.execute(query, tuple(params))
            rows = cur.fetchall()

            for row in rows:
                orders_list.append({
                    "order_id": str(row[0]),
                    "user_id": row[1],
                    "cart_id": row[2],
                    "total_amount": float(row[3]),
                    "status": row[4],
                    "invoice_url": row[5],
                    "created_at": row[6],
                    "updated_at": row[7]
                })

        logger.info(f"Successfully fetched {len(orders_list)} orders from database")
    except Exception as e:
        logger.error(f"Database query failed: {e}")
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Database query error", "details": str(e)})
        }
    finally:
        if conn:
            conn.close()

    # 3. Return JSON results
    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*"  # Enable CORS for browser frontends
        },
        "body": json.dumps(orders_list, cls=DateTimeAndUUIDEncoder, indent=2)
    }
