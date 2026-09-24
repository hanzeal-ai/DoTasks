#!/usr/bin/env bash
set -euo pipefail
: "${DEPLOY_HOST:?Set DEPLOY_HOST}"
: "${DEPLOY_KEY:?Set DEPLOY_SSH_KEY}"
: "${DEPLOY_KNOWN_HOSTS:?Set DEPLOY_KNOWN_HOSTS}"
[[ "$DEPLOY_HOST" =~ ^[A-Za-z0-9.-]+$ ]]
[[ "$#" == 3 && "$1" == site && "$2" =~ ^[a-f0-9]{40}$ && -f "$3" ]]
key_dir=$(mktemp -d)
trap 'rm -rf "$key_dir"' EXIT
chmod 700 "$key_dir"
printf '%s\n' "$DEPLOY_KEY" > "$key_dir/key"
printf '%s\n' "$DEPLOY_KNOWN_HOSTS" > "$key_dir/known_hosts"
chmod 600 "$key_dir/key" "$key_dir/known_hosts"
ssh -i "$key_dir/key" -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$key_dir/known_hosts" \
  -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
  "dotasks-deploy@$DEPLOY_HOST" "$1 $2" < "$3"
