#!/usr/bin/env bash
# ==============================================================================
# E-commerce SAGA - Deployment Automation Script
# ==============================================================================
set -euo pipefail

# Prevent Git Bash from converting paths for Windows native binaries (fixes 'unsupported protocol scheme "c"')
export MSYS_NO_PATHCONV=1

# Text format helpers
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'
INFO='\033[0;34m'

echo -e "${INFO}=== Commencing OpenFaaS Deployment Workflow ===${NC}"

# 1. Check Docker Hub Username
if [ -z "${DOCKER_HUB_USERNAME:-}" ]; then
    echo -e "${RED}Error: DOCKER_HUB_USERNAME environment variable is not set.${NC}"
    echo -e "You must define your Docker Hub username to push the compiled function images."
    echo -e "Please run the following command in your terminal before restarting:"
    echo -e "  ${INFO}export DOCKER_HUB_USERNAME=\"wass3888\"${NC}  (or your custom Docker Hub handle)"
    exit 1
else
    echo -e "Deploying images using Docker Hub username: ${GREEN}${DOCKER_HUB_USERNAME}${NC}"
fi

# 2. Check Gateway connectivity and define CLI wrapper
OPENFAAS_GATEWAY_URL="${OPENFAAS_GATEWAY_URL:-http://127.0.0.1:8080}"
GATEWAY_ARG="$OPENFAAS_GATEWAY_URL"

if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    echo -e "${INFO}Windows/Git Bash environment detected. Using dockerized faas-cli wrapper to avoid path mapping bugs.${NC}"
    
    # Map localhost/127.0.0.1 to host.docker.internal inside the dockerized faas-cli
    if [[ "$GATEWAY_ARG" == *"127.0.0.1"* || "$GATEWAY_ARG" == *"localhost"* ]]; then
        GATEWAY_ARG=$(echo "$GATEWAY_ARG" | sed -e 's/127.0.0.1/host.docker.internal/g' -e 's/localhost/host.docker.internal/g')
    fi

    # Docker wrapper function using internal POSIX filesystem to bypass Windows NTFS/OneDrive chmod issues
    faas_cli() {
        docker run --rm \
          -u root \
          -v /var/run/docker.sock:/var/run/docker.sock \
          -v "$HOME/.openfaas:/root/.openfaas_mount:ro" \
          -v "$HOME/.docker:/root/.docker" \
          -v "$PWD:/workspace-host:ro" \
          -e DOCKER_HUB_USERNAME="${DOCKER_HUB_USERNAME:-}" \
          --entrypoint sh \
          ghcr.io/openfaas/faas-cli:latest -c "
            # Install Docker CLI to communicate with the mounted docker.sock
            if ! command -v docker >/dev/null 2>&1; then
                if command -v apk >/dev/null 2>&1; then
                    apk add --no-cache docker-cli >/dev/null 2>&1
                elif command -v apt-get >/dev/null 2>&1; then
                    apt-get update >/dev/null 2>&1 && apt-get install -y docker.io >/dev/null 2>&1
                fi
            fi
            # Prepare config directory and map auth hosts (127.0.0.1/localhost -> host.docker.internal)
            mkdir -p /root/.openfaas
            if [ -d /root/.openfaas_mount ] && [ -f /root/.openfaas_mount/config.yml ]; then
                cp -r /root/.openfaas_mount/. /root/.openfaas/
                sed -i 's/127.0.0.1:8080/host.docker.internal:8080/g' /root/.openfaas/config.yml 2>/dev/null || true
                sed -i 's/localhost:8080/host.docker.internal:8080/g' /root/.openfaas/config.yml 2>/dev/null || true
            fi
            mkdir -p /workspace
            cp -r /workspace-host/. /workspace/
            cd /workspace
            faas-cli \"\$@\"
          " sh "$@"
    }
else
    faas_cli() {
        faas-cli "$@"
    }
fi

echo -e "Targeting OpenFaaS Gateway at: ${GREEN}${OPENFAAS_GATEWAY_URL}${NC}"

# Check if faas-cli is logged in
echo "Checking OpenFaaS CLI login status..."
if ! faas_cli list --gateway "$GATEWAY_ARG" >/dev/null 2>&1; then
    echo -e "${YELLOW}Warning: Cannot list functions. You might not be authenticated with OpenFaaS gateway.${NC}"
    echo -e "If deployment fails, please run: ${INFO}faas-cli login --username admin --password-stdin --gateway $OPENFAAS_GATEWAY_URL${NC}"
fi

# 3. Pull required templates
echo -e "\n${INFO}Step 1: Pulling Python HTTP runtime templates...${NC}"
faas_cli template store pull python3-http
echo -e "${GREEN}Templates successfully pulled.${NC}"

# 4. Build Docker Images
echo -e "\n${INFO}Step 2: Building Docker images for functions...${NC}"
faas_cli build -f stack.yml
echo -e "${GREEN}Build succeeded for all functions.${NC}"

# 5. Push Docker Images to Docker Hub
echo -e "\n${INFO}Step 3: Pushing Docker images to Docker Hub...${NC}"
faas_cli push -f stack.yml
echo -e "${GREEN}Push succeeded. All images uploaded to registry.${NC}"

# 6. Deploy to OpenFaaS Gateway
echo -e "\n${INFO}Step 4: Deploying serverless functions to cluster...${NC}"
faas_cli deploy -f stack.yml --gateway "$GATEWAY_ARG"
echo -e "${GREEN}Deployment command completed successfully.${NC}"

echo -e "\n${GREEN}=== OpenFaaS SAGA Functions Deployed! ===${NC}"
echo -e "Verify the deployment status with: ${INFO}faas-cli list --gateway $OPENFAAS_GATEWAY_URL${NC}"
exit 0
