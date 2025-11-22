#!/bin/bash
set -e

# UPDATED: Point to the file in the /app mount (from host) 
# instead of /etc/webhook to avoid Docker Volume masking issues.
TEMPLATE="/app/webhook.json"
CONFIG="/etc/webhook/hooks.json"

echo "⚙️  Initializing Webhook..."

if [ -z "$WEBHOOK_SECRET" ]; then
    echo "❌ Error: WEBHOOK_SECRET environment variable is missing!"
    exit 1
fi

# Verify template exists before attempting to read it
if [ ! -f "$TEMPLATE" ]; then
    echo "❌ Error: Template file not found at $TEMPLATE"
    echo "   Ensure the host directory is mounted to /app in docker-compose.yml"
    exit 1
fi

# Replace the placeholder in the template with the actual environment variable
# and write it to the active config file.
sed "s|{{WEBHOOK_SECRET}}|$WEBHOOK_SECRET|g" "$TEMPLATE" > "$CONFIG"

echo "✅ Secret injected successfully."
echo "🚀 Starting webhook listener..."

# Execute the command passed to the docker container (the webhook tool)
exec /usr/local/bin/webhook "$@"