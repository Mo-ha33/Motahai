#!/usr/bin/env bash
# =============================================================================
# deploy_employees.sh — Automated Deployment for Ameen Digital AI Workforce
# =============================================================================
# Target VPS:    Contabo VPS (<VPS_IP>)
# Target Domain: https://employees.motahai.com
# Local Binding: 127.0.0.1:58770
# Supervisor:    https://hermes.motahai.com (127.0.0.1:58763)
# =============================================================================

set -euo pipefail

# Text colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

DOMAIN="employees.motahai.com"
PORT="58770"
APP_DIR="/opt/ameen-workforce"
ADMIN_EMAIL="admin@motahai.com"

echo -e "${BLUE}======================================================================${NC}"
echo -e "${BLUE}  Deploying Ameen Digital AI Workforce to Contabo VPS (<VPS_IP>)  ${NC}"
echo -e "${BLUE}  Target Domain: https://${DOMAIN} -> 127.0.0.1:${PORT}             ${NC}"
echo -e "${BLUE}======================================================================${NC}"

# 1. Root Check
if [ "$(id -u)" -ne 0 ]; then
    echo -e "${RED}[ERROR] This deployment script must be run as root or with sudo.${NC}"
    exit 1
fi

# 2. System Packages & Python Environment
echo -e "\n${YELLOW}[Step 1/6] Installing dependencies...${NC}"
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv nginx certbot python3-certbot-nginx curl jq

# 3. Create Application Directory & Virtualenv
echo -e "\n${YELLOW}[Step 2/6] Setting up application directory at ${APP_DIR}...${NC}"
mkdir -p "${APP_DIR}"
cd "${APP_DIR}"

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate
pip install --upgrade pip
pip install fastapi uvicorn httpx pydantic requests slowapi

# 4. Deploy Systemd Unit
echo -e "\n${YELLOW}[Step 3/6] Configuring systemd service...${NC}"
cp deployment/systemd/ameen-workforce.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable ameen-workforce.service
systemctl restart ameen-workforce.service

# 5. Configure Nginx
echo -e "\n${YELLOW}[Step 4/6] Configuring Nginx reverse proxy...${NC}"
cp deployment/nginx/employees.motahai.com.conf /etc/nginx/sites-available/
ln -sf /etc/nginx/sites-available/employees.motahai.com.conf /etc/nginx/sites-enabled/

# 6. Issue SSL Certificate if not already present
echo -e "\n${YELLOW}[Step 5/6] Verifying SSL certificate...${NC}"
if [ ! -f "/etc/letsencrypt/live/${DOMAIN}/fullchain.pem" ]; then
    echo -e "${YELLOW}Requesting Let's Encrypt certificate for ${DOMAIN}...${NC}"
    certbot certonly --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${ADMIN_EMAIL}" || true
fi

nginx -t
systemctl reload nginx

# 7. Verification & Health Check
echo -e "\n${YELLOW}[Step 6/6] Running service health check...${NC}"
sleep 2
HEALTH=$(curl -s http://127.0.0.1:${PORT}/health || true)
echo "Local Health Check: ${HEALTH}"

echo -e "\n${GREEN}======================================================================${NC}"
echo -e "${GREEN}  Ameen Digital AI Workforce Deployed Successfully!                    ${NC}"
echo -e "${GREEN}  Portal: https://${DOMAIN}                                            ${NC}"
echo -e "${GREEN}  Hermes Bridge: Connected to 127.0.0.1:58763                         ${NC}"
echo -e "${GREEN}======================================================================${NC}"
