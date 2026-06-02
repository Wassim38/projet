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
                
    # Fallback to env var format (e.g. API_CREDENTIALS_PAYMENT_API_KEY)
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
    OpenFaaS asynchronous handler logic.
    Expects order details from NATS queue: { order_id, user_id, cart_id, total_amount, items }
    """
    logger.info("Received request in payment-reconciler")
    
    # 1. Parse Event Body
    try:
        body = event.body
        if isinstance(body, bytes):
            body = body.decode('utf-8')
        
        if not body:
            logger.error("Empty payload received in payment-reconciler")
            return {"statusCode": 400, "body": "Empty payload"}

        payload = json.loads(body)
    except Exception as e:
        logger.error(f"Failed to parse event JSON: {e}")
        return {"statusCode": 400, "body": f"Invalid JSON: {str(e)}"}

    order_id = payload.get("order_id")
    total_amount = payload.get("total_amount")
    cart_id = payload.get("cart_id")
    user_id = payload.get("user_id")

    if not order_id or total_amount is None:
        logger.error("Missing order_id or total_amount in payload")
        return {"statusCode": 400, "body": "Missing order_id or total_amount"}

    # 2. Retrieve Payment Credentials from Secret
    payment_key = read_secret("api-credentials", "payment-api-key")
    if not payment_key:
        logger.error("Payment API key could not be retrieved from secrets")
        return {"statusCode": 500, "body": "Configuration error: payment API key missing"}
    
    # Mask API key for logs security
    masked_key = payment_key[:6] + "..." if len(payment_key) > 6 else "***"
    logger.info(f"Payment reconciler API Key validated: {masked_key}")

    # 3. Simulate Payment reconciliation
    logger.info(f"Processing payment simulation for order {order_id} of amount EUR {total_amount}...")
    
    # Deterministic simulation rules:
    # - If total_amount is >= 1000.0, fail payment (limit check)
    # - If cart_id contains "fail", fail payment (explicit test)
    # - Otherwise, succeed.
    payment_success = True
    failure_reason = ""

    if float(total_amount) >= 1000.0:
        payment_success = False
        failure_reason = "Payment rejected: Total amount exceeds limit of 1000.0"
    elif cart_id and "fail" in str(cart_id).lower():
        payment_success = False
        failure_reason = "Payment rejected: Card simulated failure pattern (cart_id contains 'fail')"

    new_status = "PAID" if payment_success else "FAILED"
    logger.info(f"Payment result for {order_id}: SUCCESS={payment_success}. Reason: {failure_reason or 'None'}")

    # 4. Update Database State Machine
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # First, check if order exists and is in PENDING_PAYMENT
            cur.execute("SELECT status FROM orders WHERE order_id = %s FOR UPDATE;", (order_id,))
            row = cur.fetchone()
            if not row:
                logger.error(f"Order {order_id} not found in database")
                return {"statusCode": 404, "body": f"Order {order_id} not found"}
            
            current_status = row[0]
            if current_status != "PENDING_PAYMENT":
                logger.warning(f"Order {order_id} is already in state '{current_status}'. Saga skip.")
                return {"statusCode": 200, "body": f"Order already processed in state {current_status}"}

            # Update order status
            cur.execute(
                "UPDATE orders SET status = %s WHERE order_id = %s;",
                (new_status, order_id)
            )
            conn.commit()
            logger.info(f"Updated order {order_id} state to '{new_status}' in PostgreSQL")
    except Exception as e:
        logger.error(f"Database error during order update: {e}")
        if conn:
            conn.rollback()
        return {"statusCode": 500, "body": f"Database error: {str(e)}"}
    finally:
        if conn:
            conn.close()

    # 5. Route SAGA Flow
    if payment_success:
        # If paid, trigger invoice-archiver asynchronously
        gateway_url = os.environ.get("OPENFAAS_GATEWAY_URL", "http://gateway.openfaas:8080")
        async_url = f"{gateway_url}/async-function/invoice-archiver"
        
        invoice_payload = {
            "order_id": order_id,
            "user_id": user_id,
            "cart_id": cart_id,
            "total_amount": float(total_amount),
            "items": payload.get("items", [])
        }

        try:
            logger.info(f"Triggering invoice-archiver asynchronously at: {async_url}")
            response = requests.post(async_url, json=invoice_payload, timeout=5)
            logger.info(f"Async call to invoice-archiver response status: {response.status_code}")
        except Exception as e:
            logger.error(f"Failed to trigger invoice-archiver asynchronously: {e}")
            # In a real system, we'd queue a retry.
    else:
        logger.info(f"Payment failed for Order {order_id}. Saga workflow terminated.")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "order_id": order_id,
            "payment_success": payment_success,
            "status": new_status,
            "reason": failure_reason
        })
    }
