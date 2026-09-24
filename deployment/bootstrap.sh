#!/usr/bin/env bash
# Operator-only installation, after independent review. Does not restart applications.
set -euo pipefail
[[ $(id -u) == 0 ]]
: "${DEPLOY_PUBLIC_KEY:?Provide the dedicated Ed25519 deploy public key}"
[[ "$DEPLOY_PUBLIC_KEY" =~ ^ssh-ed25519\ [A-Za-z0-9+/=]+(\ .*)?$ ]]
[[ "$DEPLOY_PUBLIC_KEY" != *$'\n'* ]]
source_dir=$(cd -- "$(dirname -- "$0")" && pwd)
account=dotasks-deploy
helpers=/usr/local/libexec/dotasks-deploy
[[ -x /usr/bin/python3.11 && -x /usr/bin/sudo ]]
[[ -f /home/admin/dotasks/.env && -f /home/admin/dotasks/.dotasks-cloud/compose.yaml ]]
if id "$account" >/dev/null 2>&1 || [[ -e "$helpers" || -e /etc/sudoers.d/dotasks-deploy ]]; then
  echo 'Already provisioned; inspect before updating privileged deployment files.' >&2
  exit 1
fi
useradd --system --home-dir /var/lib/dotasks-deploy --shell /bin/bash "$account"
install -d -m 755 -o root -g root /var/lib/dotasks-deploy /var/lib/dotasks-deploy/.ssh "$helpers"
install -m 755 -o root -g root "$source_dir/ssh-entry.py" "$source_dir/receive.py" "$source_dir/activate.py" "$helpers/"
install -m 600 -o root -g root /home/admin/dotasks/.dotasks-cloud/compose.yaml "$helpers/compose.yaml"
printf 'restrict,command="%s/ssh-entry.py" %s\n' "$helpers" "$DEPLOY_PUBLIC_KEY" > /var/lib/dotasks-deploy/.ssh/authorized_keys
chmod 644 /var/lib/dotasks-deploy/.ssh/authorized_keys
printf '%s\n' 'dotasks-deploy ALL=(root) NOPASSWD: /usr/local/libexec/dotasks-deploy/receive.py *' > /etc/sudoers.d/dotasks-deploy
chmod 440 /etc/sudoers.d/dotasks-deploy
visudo -cf /etc/sudoers.d/dotasks-deploy
echo 'Receiver installed; configure verified host key and keep DEPLOY_ENABLED=false until tested.'
