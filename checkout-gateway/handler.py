import os
import json
import logging
import psycopg2
import requests

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

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

    logger.info(f"Connecting to database at {host}:{port} for db {dbname}...")
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
    OpenFaaS handler logic.
    Expects a JSON payload: { cart_id, user_id, items, total_amount }
    """
    logger.info("Received request in checkout-gateway")
    
    # 1. Parse Event Body
    try:
        body = event.body
        if isinstance(body, bytes):
            body = body.decode('utf-8')
        
        # If payload is empty or not JSON, handle it
        if not body:
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "Empty request body"})
            }
            
        payload = json.loads(body)
    except Exception as e:
        logger.error(f"Failed to parse request JSON: {e}")
        return {
            "statusCode": 400,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Invalid JSON format", "details": str(e)})
        }

    # 2. Schema Validation
    required_fields = ["cart_id", "user_id", "total_amount"]
    for field in required_fields:
        if field not in payload:
            logger.warning(f"Validation failed: missing field '{field}'")
            return {
                "statusCode": 400,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": f"Missing required field: '{field}'"})
            }

    cart_id = payload["cart_id"]
    user_id = payload["user_id"]
    total_amount = payload["total_amount"]
    items = payload.get("items", [])

    # 3. Insert Order in DB (PENDING_PAYMENT)
    conn = None
    order_id = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            query = """
                INSERT INTO orders (user_id, cart_id, total_amount, status)
                VALUES (%s, %s, %s, 'PENDING_PAYMENT')
                RETURNING order_id, status, created_at;
            """
            cur.execute(query, (user_id, cart_id, total_amount))
            row = cur.fetchone()
            order_id = str(row[0])
            status = row[1]
            created_at = row[2].isoformat()
            conn.commit()
            
        logger.info(f"Successfully inserted order {order_id} with status {status}")
    except Exception as e:
        logger.error(f"Database insertion failed: {e}")
        if conn:
            conn.rollback()
        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"error": "Database error", "details": str(e)})
        }
    finally:
        if conn:
            conn.close()
            logger.info("Database connection closed")

    # 4. Trigger payment-reconciler Asynchronously (NATS worker)
    # The default OpenFaaS gateway internal address is gateway.openfaas:8080.
    # We allow overriding via env var for testing flexibility.
    gateway_url = os.environ.get("OPENFAAS_GATEWAY_URL", "http://gateway.openfaas:8080")
    async_url = f"{gateway_url}/async-function/payment-reconciler"

    payment_payload = {
        "order_id": order_id,
        "user_id": user_id,
        "cart_id": cart_id,
        "total_amount": float(total_amount),
        "items": items
    }

    try:
        logger.info(f"Invoking payment-reconciler asynchronously at: {async_url}")
        # Note: OpenFaaS routes requests to /async-function/ to the NATS Queue-Worker
        response = requests.post(
            async_url,
            json=payment_payload,
            headers={"X-Callback-Url": ""}, # Can be used if callbacks are required
            timeout=5
        )
        logger.info(f"Async call status code: {response.status_code}")
    except Exception as e:
        # In a real SAGA, we should have a retry mechanism or compensating transaction.
        # We will log the error but still return the created order (the client can query history).
        logger.error(f"Failed to trigger payment-reconciler asynchronously: {e}")

    # 5. Return HTTP 201 to client
    return {
        "statusCode": 201,
        "headers": {
            "Content-Type": "application/json"
        },
        "body": json.dumps({
            "order_id": order_id,
            "status": "PENDING_PAYMENT",
            "message": "Order initialised. Payment reconciliation started in background.",
            "created_at": created_at
        })
    }
