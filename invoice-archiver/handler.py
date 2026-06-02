import os
import json
import logging
from datetime import datetime
import psycopg2
import boto3
from botocore.client import Config
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
                
    # Fallback to env var format (e.g. S3_CREDENTIALS_ENDPOINT)
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
    logger.info("Received request in invoice-archiver")
    
    # 1. Parse Event Body
    try:
        body = event.body
        if isinstance(body, bytes):
            body = body.decode('utf-8')
        
        if not body:
            logger.error("Empty payload received in invoice-archiver")
            return {"statusCode": 400, "body": "Empty payload"}

        payload = json.loads(body)
    except Exception as e:
        logger.error(f"Failed to parse event JSON: {e}")
        return {"statusCode": 400, "body": f"Invalid JSON: {str(e)}"}

    order_id = payload.get("order_id")
    if not order_id:
        logger.error("Missing order_id in payload")
        return {"statusCode": 400, "body": "Missing order_id"}

    # 2. Fetch Order from DB & Validate State
    conn = None
    order_data = {}
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT order_id, user_id, cart_id, total_amount, status, created_at FROM orders WHERE order_id = %s FOR UPDATE;",
                (order_id,)
            )
            row = cur.fetchone()
            if not row:
                logger.error(f"Order {order_id} not found in database")
                return {"statusCode": 404, "body": f"Order {order_id} not found"}
            
            order_data = {
                "order_id": str(row[0]),
                "user_id": row[1],
                "cart_id": row[2],
                "total_amount": float(row[3]),
                "status": row[4],
                "created_at": row[5]
            }

            if order_data["status"] != "PAID":
                logger.warning(f"Order {order_id} is in status '{order_data['status']}', expected 'PAID'. SAGA flow abort.")
                return {"statusCode": 400, "body": f"Order {order_id} not in PAID status"}
    except Exception as e:
        logger.error(f"Database read failed for order {order_id}: {e}")
        return {"statusCode": 500, "body": f"Database read error: {str(e)}"}
    finally:
        if conn and conn.closed == 0:
            conn.close()

    # 3. Generate Structured Invoice Document
    now = datetime.utcnow()
    year_month = now.strftime("%Y-%m")
    invoice_date = now.isoformat() + "Z"
    
    invoice_doc = {
        "invoice_id": f"INV-{order_id[:8].upper()}-{now.strftime('%Y%m%d')}",
        "order_id": order_data["order_id"],
        "user_id": order_data["user_id"],
        "cart_id": order_data["cart_id"],
        "total_amount": order_data["total_amount"],
        "items": payload.get("items", []),
        "currency": "EUR",
        "billing_date": invoice_date,
        "payment_status": "PAID"
    }

    # 4. Upload to S3/MinIO
    s3_endpoint = read_secret("s3-credentials", "endpoint", "http://host.docker.internal:9000")
    s3_access_key = read_secret("s3-credentials", "access_key", "minioadmin")
    s3_secret_key = read_secret("s3-credentials", "secret_key", "minioadmin")
    s3_bucket = read_secret("s3-credentials", "bucket_name", "invoices")

    s3_key = f"factures/{year_month}/invoice_{order_id}.json"
    logger.info(f"Uploading invoice to MinIO at bucket '{s3_bucket}', key '{s3_key}'...")

    try:
        # Connect to MinIO / S3
        s3 = boto3.client(
            's3',
            endpoint_url=s3_endpoint,
            aws_access_key_id=s3_access_key,
            aws_secret_access_key=s3_secret_key,
            config=Config(signature_version='s3v4'),
            region_name='us-east-1' # standard region default for minio
        )
        
        # Put JSON object
        s3.put_object(
            Bucket=s3_bucket,
            Key=s3_key,
            Body=json.dumps(invoice_doc, indent=4),
            ContentType='application/json'
        )
        logger.info(f"Successfully uploaded invoice {s3_key} to MinIO")
    except Exception as e:
        logger.error(f"MinIO/S3 upload failed for invoice {s3_key}: {e}")
        return {"statusCode": 500, "body": f"MinIO upload error: {str(e)}"}

    # 5. Generate Access URL and Update Status to INVOICED
    # If the endpoint is internal to the cluster (host.docker.internal), we also generate a host-friendly URL for local download.
    user_download_url = f"http://localhost:9000/{s3_bucket}/{s3_key}"
    internal_s3_url = f"{s3_endpoint}/{s3_bucket}/{s3_key}"
    
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET status = 'INVOICED', invoice_url = %s WHERE order_id = %s;",
                (user_download_url, order_id)
            )
            conn.commit()
            logger.info(f"Updated order {order_id} state to 'INVOICED' with invoice URL in PostgreSQL")
    except Exception as e:
        logger.error(f"Database update failed for order {order_id}: {e}")
        if conn:
            conn.rollback()
        return {"statusCode": 500, "body": f"Database update error: {str(e)}"}
    finally:
        if conn:
            conn.close()

    # 6. Trigger Business Alert: Webhook Discord/Slack
    discord_webhook_url = read_secret("api-credentials", "webhook-url")
    if discord_webhook_url and "dummy_webhook" not in discord_webhook_url:
        logger.info(f"Sending invoice notification alert to Discord Webhook...")
        discord_payload = {
            "username": "E-Commerce SAGA Bot",
            "avatar_url": "https://img.icons8.com/color/96/000000/shopping-cart.png",
            "content": f"🚀 **Order Processed and Billed Successfully! (SAGA Complete)**",
            "embeds": [
                {
                    "title": f"Facture #{invoice_doc['invoice_id']}",
                    "color": 3066993,  # Vibrant Green color
                    "fields": [
                        {"name": "Order UUID", "value": f"`{order_id}`", "inline": False},
                        {"name": "User ID", "value": f"`{order_data['user_id']}`", "inline": True},
                        {"name": "Cart ID", "value": f"`{order_data['cart_id']}`", "inline": True},
                        {"name": "Total Amount", "value": f"**{order_data['total_amount']:.2f} EUR**", "inline": True},
                        {"name": "Invoice PDF/JSON URL", "value": f"[Download Invoice from MinIO]({user_download_url})", "inline": False}
                    ],
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "footer": {
                        "text": "SAGA Event-Driven Transaction Framework"
                    }
                }
            ]
        }
        try:
            res = requests.post(discord_webhook_url, json=discord_payload, timeout=5)
            logger.info(f"Discord alert sent. Webhook response code: {res.status_code}")
        except Exception as e:
            logger.error(f"Failed sending webhook notification to Discord: {e}")
    else:
        logger.warning("Discord webhook URL not configured or is dummy. Skipping notification.")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "order_id": order_id,
            "status": "INVOICED",
            "invoice_url": user_download_url,
            "s3_path": s3_key,
            "message": "Saga transaction successfully finalized and invoice generated."
        })
    }
