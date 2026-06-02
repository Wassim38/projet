#!/usr/bin/env bash
# ==============================================================================
# E-commerce SAGA - Aggressive Load Testing Script
# ==============================================================================
set -euo pipefail

# Text format helpers
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'
INFO='\033[0;34m'

# Configuration
GATEWAY_URL="${OPENFAAS_GATEWAY_URL:-http://127.0.0.1:8080}"
TARGET_URL="${GATEWAY_URL}/function/checkout-gateway"
CONCURRENCY=30
DURATION="15s"
TOTAL_REQUESTS=400

echo -e "${INFO}=== Initialising SAGA Purchase Funnel Load Test ===${NC}"
echo -e "Target endpoint: ${GREEN}${TARGET_URL}${NC}"
echo -e "Parameters: Concurrency=${CONCURRENCY}, Duration=${DURATION} / Total=${TOTAL_REQUESTS} requests\n"

# JSON Payload data
PAYLOAD_FILE=$(mktemp /tmp/saga_payload.XXXXXX.json)
cat <<EOF > "$PAYLOAD_FILE"
{
  "cart_id": "cart_loadtest_2026",
  "user_id": "user_loadtest_student",
  "total_amount": 149.99,
  "items": [
    {
      "name": "Cloud Native Serverless Architecture Book",
      "quantity": 1,
      "price": 89.99
    },
    {
      "name": "OpenFaaS Developer Course",
      "quantity": 1,
      "price": 60.00
    }
  ]
}
EOF

# Clean up temp file on exit
trap 'rm -f "$PAYLOAD_FILE"' EXIT

# Detect and execute the best available HTTP load testing tool
if command -v hey >/dev/null 2>&1; then
    echo -e "${GREEN}[hey] detected. Running aggressive load test...${NC}"
    # hey is the modern alternative to ab, supports HTTP/2 and concurrent loads natively
    hey -z "$DURATION" -c "$CONCURRENCY" -m POST -H "Content-Type: application/json" -D "$PAYLOAD_FILE" "$TARGET_URL"

elif command -v ab >/dev/null 2>&1; then
    echo -e "${GREEN}[ab] (Apache Bench) detected. Running benchmark load test...${NC}"
    # Run ab with content type json
    ab -n "$TOTAL_REQUESTS" -c "$CONCURRENCY" -T "application/json" -p "$PAYLOAD_FILE" "$TARGET_URL"

else
    echo -e "${YELLOW}[hey/ab] not found. Falling back to native parallelized curl loop...${NC}"
    echo -e "Simulating ${CONCURRENCY} concurrent client pipelines executing ${TOTAL_REQUESTS} requests..."
    
    start_time=$(date +%s)
    
    # Spawn curl jobs in background
    for ((i=1; i<=TOTAL_REQUESTS; i++)); do
        # Generate slightly randomized cart IDs to prevent caching
        curl -s -o /dev/null -w "%{http_code}" \
          -X POST \
          -H "Content-Type: application/json" \
          -d "{\"cart_id\":\"cart_curl_$i\",\"user_id\":\"user_curl_$i\",\"total_amount\":12.50,\"items\":[]}" \
          "$TARGET_URL" &
        
        # Limit concurrency using simple job control check
        if [ $((i % CONCURRENCY)) -eq 0 ]; then
            wait
        fi
    done
    wait
    
    end_time=$(date +%s)
    duration=$((end_time - start_time))
    echo -e "\n${GREEN}Completed ${TOTAL_REQUESTS} requests in ${duration} seconds using curl daemon pool.${NC}"
fi

echo -e "\n${GREEN}=== Load Test Execution Complete! ===${NC}"
echo -e "Check the logs to witness NATS queue worker piling and invoice-archiver auto-scaling:"
echo -e "  1. Watch function scaling count: ${INFO}kubectl get deploy -n openfaas-fn invoice-archiver -w${NC}"
echo -e "  2. Count orders processed:       ${INFO}docker exec -it saga-postgres psql -U postgres -d saga_ecommerce -c \"SELECT status, COUNT(*) FROM orders GROUP BY status;\"${NC}"
exit 0
