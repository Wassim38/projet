#!/usr/bin/env bash
# ==============================================================================
# E-commerce SAGA - OpenFaaS Environment Verification Script
# ==============================================================================
set -euo pipefail

# Text format helper
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color
INFO='\033[0;34m'

echo -e "${INFO}=== Starting Environment Audit for OpenFaaS E-Commerce SAGA ===${NC}"

# 1. Verify Docker Daemon
echo -n "Checking Docker daemon... "
if ! docker info >/dev/null 2>&1; then
    echo -e "${RED}FAILED${NC}"
    echo -e "${RED}Error: Docker daemon is not running or accessible. Please start Docker Desktop/Daemon.${NC}"
    exit 1
else
    echo -e "${GREEN}OK${NC}"
fi

# 2. Verify Kubernetes Cluster
echo -n "Checking Kubernetes cluster connectivity... "
if ! kubectl cluster-info >/dev/null 2>&1; then
    echo -e "${RED}FAILED${NC}"
    echo -e "${RED}Error: Unable to connect to a Kubernetes cluster. Check if minikube, kind, or Docker Desktop Kubernetes is running.${NC}"
    exit 1
else
    echo -e "${GREEN}OK${NC}"
fi

# 3. Verify Helm CLI
echo -n "Checking Helm CLI installation... "
if ! command -v helm >/dev/null 2>&1; then
    echo -e "${RED}FAILED${NC}"
    echo -e "${RED}Error: Helm CLI is not installed. Please install Helm (https://helm.sh/).${NC}"
    exit 1
else
    echo -e "${GREEN}OK${NC} ($(helm version --short))"
fi

# 4. Verify OpenFaaS CLI
echo -n "Checking OpenFaaS CLI (faas-cli)... "
if ! command -v faas-cli >/dev/null 2>&1; then
    echo -e "${RED}FAILED${NC}"
    echo -e "${RED}Error: faas-cli is not installed. Please install OpenFaaS CLI (https://github.com/openfaas/faas-cli).${NC}"
    exit 1
else
    echo -e "${GREEN}OK${NC} ($(faas-cli version --short-version))"
fi

# 5. Verify Docker Hub Authentication
echo -n "Checking Docker Hub authentication... "
DOCKER_CONFIG_FILE="$HOME/.docker/config.json"
if [ -f "$DOCKER_CONFIG_FILE" ]; then
    # Search for auths key and check if it has entries
    if grep -q '"auths"' "$DOCKER_CONFIG_FILE" && grep -A 2 '"auths"' "$DOCKER_CONFIG_FILE" | grep -q '"auth"'; then
        echo -e "${GREEN}OK${NC} (Credentials found in $DOCKER_CONFIG_FILE)"
    else
        echo -e "${RED}FAILED${NC}"
        echo -e "${RED}Error: Found Docker config at $DOCKER_CONFIG_FILE but no credentials under 'auths'. Please run 'docker login'.${NC}"
        exit 1
    fi
else
    echo -e "${RED}FAILED${NC}"
    echo -e "${RED}Error: Docker configuration file not found at $DOCKER_CONFIG_FILE. Please run 'docker login' to log into Docker Hub.${NC}"
    exit 1
fi

echo -e "\n${GREEN}=== Environment Audit Successful! All pre-requisites are met. ===${NC}"
exit 0
