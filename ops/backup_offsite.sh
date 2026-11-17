#!/bin/bash
# PayCensus off-site backup -> Singapore server (43.159.62.39)
# Daily snapshot with hardlink dedup (rsync --link-dest), keep 14 days.
# Installed: /opt/paycensus/backup_offsite.sh ; cron: daily.
set -u
SG_HOST="43.159.62.39"
SG_USER="ubuntu"
SG_DEST="/home/ubuntu/paycensus-backup"
KEY="$HOME/.ssh/id_ed25519_backup"
KEEP=14
LOCK=/tmp/paycensus_backup.lock

exec 9>"$LOCK" || exit 1
flock -n 9 || { echo "another backup running, skip"; exit 0; }

LOG=/var/lib/paycensus/backup_offsite.log
CM=/tmp/paycensus_backup_cm.sock
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -o BatchMode=yes -o ControlMaster=auto -o ControlPath=$CM -o ControlPersist=120"
TODAY=$(date +%F)
# open one multiplexed connection for the whole run (avoids rapid-reconnect resets)
$SSH "$SG_USER@$SG_HOST" "mkdir -p $SG_DEST/$TODAY" || { echo "SG unreachable $(date -u)" >> "$LOG"; exit 1; }
LOG=/var/lib/paycensus/backup_offsite.log
{
  echo "=== backup start $(date -u '+%F %T UTC') ==="
  # 1. irreplaceable state (databases, anchoring, states)
  rsync -az --delete -e "$SSH" /var/lib/paycensus/ "$SG_USER@$SG_HOST:$SG_DEST/$TODAY/varlib/"
  # 2. webroots (site + published data)
  rsync -az --delete -e "$SSH" /www/wwwroot/paycensus/ "$SG_USER@$SG_HOST:$SG_DEST/$TODAY/www_paycensus/"
  rsync -az --delete -e "$SSH" /www/wwwroot/trust/ "$SG_USER@$SG_HOST:$SG_DEST/$TODAY/www_trust/"
  # 3. scripts + configs (secret dotfiles EXCLUDED — go in the encrypted bundle below)
  rsync -az --delete -e "$SSH" \
    --exclude=.trust_identity --exclude=.x_credentials \
    --exclude=.smtp_hello --exclude=.smtp_trust --exclude=.smtp_bd --exclude=.smtp_foxreply \
    --exclude=.trust_stripe --exclude=.pypi_token --exclude=.backup_passphrase \
    /opt/paycensus/ "$SG_USER@$SG_HOST:$SG_DEST/$TODAY/opt/"
  # 3b. secrets bundle: tar (sudo, root-readable files) -> openssl encrypt (passphrase
  #     lives on main server (root 600) AND in the user's local credential folder).
  #     Recovery: decrypt with 'openssl enc -d -aes-256-cbc -pbkdf2 -pass file:...' then tar xzf.
  SECRETS=".trust_identity .x_credentials .smtp_hello .smtp_trust .smtp_bd .smtp_foxreply .trust_stripe .pypi_token"
  sudo tar czf /tmp/pc_secrets.tgz --ignore-failed-read -C /opt/paycensus $SECRETS 2>/dev/null || true
  sudo chown ubuntu /tmp/pc_secrets.tgz
  sudo openssl enc -aes-256-cbc -pbkdf2 -salt \
    -in /tmp/pc_secrets.tgz -out /tmp/pc_secrets.tgz.enc \
    -pass file:/opt/paycensus/.backup_passphrase
  sudo chown ubuntu /tmp/pc_secrets.tgz.enc
  rsync -az -e "$SSH" /tmp/pc_secrets.tgz.enc "$SG_USER@$SG_HOST:$SG_DEST/$TODAY/opt/"
  shred -u /tmp/pc_secrets.tgz 2>/dev/null || rm -f /tmp/pc_secrets.tgz
  rm -f /tmp/pc_secrets.tgz.enc
  # 4. nginx config (small but painful to rebuild)
  rsync -az -e "$SSH" /etc/nginx/nginx.conf "$SG_USER@$SG_HOST:$SG_DEST/$TODAY/nginx/"
  rsync -az -e "$SSH" /etc/nginx/sites-enabled/ "$SG_USER@$SG_HOST:$SG_DEST/$TODAY/nginx/sites-enabled/"
  # 5. retention: keep last $KEEP day dirs
  $SSH "$SG_USER@$SG_HOST" "cd $SG_DEST && ls -1d 20* 2>/dev/null | sort | head -n -$KEEP | xargs -r rm -rf"
  # verify: newest db + encrypted secrets bundle exist on SG side
  $SSH "$SG_USER@$SG_HOST" "test -f $SG_DEST/$TODAY/varlib/census.db -a -f $SG_DEST/$TODAY/opt/pc_secrets.tgz.enc && echo VERIFY_OK || echo VERIFY_FAIL"
  echo "=== backup end $(date -u '+%F %T UTC') ==="
} >> "$LOG" 2>&1
tail -3 "$LOG" | grep -q VERIFY_OK || echo "backup verify FAILED $(date -u)" >> "$LOG"
