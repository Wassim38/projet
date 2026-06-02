#!/usr/bin/env bash
# ==============================================================================
# E-commerce SAGA - Infrastructure Setup Script (Postgres, MinIO & OpenFaaS)
# ==============================================================================
set -euo pipefail

# Text format helpers
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'
INFO='\033[0;34m'

echo -e "${INFO}=== Deploying PostgreSQL & MinIO Docker Containers ===${NC}"

# Stop and remove existing containers if they exist to be idempotent
docker rm -f saga-postgres saga-minio >/dev/null 2>&1 || true

# 1. Spin up PostgreSQL
echo "Starting PostgreSQL container (saga-postgres)..."
docker run -d \
  --name saga-postgres \
  -p 5432:5432 \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres-password \
  -e POSTGRES_DB=saga_ecommerce \
  --restart unless-stopped \
  postgres:15-alpine

# Wait for PostgreSQL to become ready
echo "Waiting for PostgreSQL to be healthy..."
until docker exec saga-postgres pg_isready -U postgres -d saga_ecommerce >/dev/null 2>&1; do
  echo -n "."
  sleep 1
done
echo -e "\n${GREEN}PostgreSQL is ready!${NC}"

# Execute SQL init script
echo "Initializing PostgreSQL schema..."
if [ -f "database/init.sql" ]; then
  docker exec -i saga-postgres psql -U postgres -d saga_ecommerce < database/init.sql
  echo -e "${GREEN}Database initialized successfully.${NC}"
else
  echo -e "${RED}Error: database/init.sql not found! Schema was not initialized.${NC}"
  exit 1
fi

# 2. Spin up MinIO
echo "Starting MinIO container (saga-minio)..."
docker run -d \
  --name saga-minio \
  -p 9000:9000 \
  -p 9001:9001 \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=minioadmin \
  --restart unless-stopped \
  minio/minio server /data --console-address ":9001"

# Wait for MinIO to become ready
echo "Waiting for MinIO to be healthy..."
until curl -s --fail http://localhost:9000/minio/health/live >/dev/null 2>&1; do
  echo -n "."
  sleep 1
done
echo -e "\n${GREEN}MinIO is ready!${NC}"

# Create MinIO S3 bucket 'invoices'
echo "Configuring MinIO client & creating 'invoices' bucket..."
# Run docker mc image with custom entrypoint to execute multiple mc command calls sequentially
docker run --rm --net=host --entrypoint sh minio/mc:latest -c "
  mc alias set localminio http://localhost:9000 minioadmin minioadmin &&
  mc mb localminio/invoices || true
"
echo -e "${GREEN}Bucket 'invoices' created successfully.${NC}"

# 3. Setup OpenFaaS in Kubernetes
echo -e "\n${INFO}=== Setting up OpenFaaS & Kubernetes Resources ===${NC}"

# Create namespaces
echo "Creating Kubernetes namespaces..."
kubectl create namespace openfaas --dry-run=client -o yaml | kubectl apply -f -
kubectl create namespace openfaas-fn --dry-run=client -o yaml | kubectl apply -f -

# Check if OpenFaaS Helm chart is already deployed
if ! helm list -n openfaas | grep -q "openfaas"; then
  echo "OpenFaaS not found. Installing OpenFaaS via Helm..."
  
  # Generate random password for OpenFaaS Gateway basic-auth
  OPENFAAS_PASSWORD=$(head -c 12 /dev/urandom | shasum | cut -d' ' -f1)
  
  kubectl -n openfaas create secret generic basic-auth \
    --from-literal=basic-auth-user=admin \
    --from-literal=basic-auth-password="$OPENFAAS_PASSWORD" \
    --dry-run=client -o yaml | kubectl apply -f -

  helm repo add openfaas https://openfaas.github.io/faas-netes/
  helm repo update

  helm upgrade --install openfaas openfaas/openfaas \
    --namespace openfaas \
    --set basicAuth=true \
    --set queueWorker.maxInflight=1
    
  echo -e "${GREEN}OpenFaaS installed with password: $OPENFAAS_PASSWORD${NC}"
else
  echo -e "${GREEN}OpenFaaS already installed in namespace 'openfaas'. Skipping installation.${NC}"
fi

# 4. Create Kubernetes Secrets inside openfaas-fn
echo -e "\n${INFO}=== Creating Secrets inside OpenFaaS Functions Namespace (openfaas-fn) ===${NC}"

# Database Secrets
kubectl create secret generic db-credentials \
  --from-literal=host="host.docker.internal" \
  --from-literal=port="5432" \
  --from-literal=user="postgres" \
  --from-literal=password="postgres-password" \
  --from-literal=dbname="saga_ecommerce" \
  -n openfaas-fn --dry-run=client -o yaml | kubectl apply -f -

# S3 Secrets
kubectl create secret generic s3-credentials \
  --from-literal=endpoint="http://host.docker.internal:9000" \
  --from-literal=access_key="minioadmin" \
  --from-literal=secret_key="minioadmin" \
  --from-literal=bucket_name="invoices" \
  -n openfaas-fn --dry-run=client -o yaml | kubectl apply -f -

# API Secrets (Discord Webhook & Payment API Key)
# Default discord webhook is configured with a dummy value.
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-https://discord.com/api/webhooks/dummy_webhook}"
kubectl create secret generic api-credentials \
  --from-literal=payment-api-key="sk_test_mock_saga_key_2026" \
  --from-literal=webhook-url="$DISCORD_WEBHOOK_URL" \
  -n openfaas-fn --dry-run=client -o yaml | kubectl apply -f -

echo -e "\n${GREEN}=== Infrastructure and Secret Setup Complete! ===${NC}"
echo -e "You can modify your secrets inside K8s if you have a real Discord webhook url via:"
echo -e "  kubectl create secret generic api-credentials --from-literal=payment-api-key=sk_test_123 --from-literal=webhook-url=YOUR_URL -n openfaas-fn --dry-run=client -o yaml | kubectl apply -f -"
