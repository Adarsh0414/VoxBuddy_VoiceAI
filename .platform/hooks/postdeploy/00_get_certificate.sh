#!/usr/bin/env bash
# Runs after every deploy (platform hook, last step of the deploy
# workflow, after the app server + nginx proxy have been (re)started).
# Idempotent and self-healing: safe to re-run on every deploy, and it
# rewrites the 443 server block every time, so it also survives EB
# regenerating its own managed nginx config on each deploy.
#
# Design note (why this does NOT use `certbot --nginx`):
# EB's default nginx server block (conf.d/elasticbeanstalk/00_application.conf)
# has `listen 80 default_server;` but no `server_name` directive. Certbot's
# --nginx plugin selects which server block to edit by matching
# server_name to -d $DOMAIN, and its fallback behaviour against a
# server_name-less default_server block is not reliably documented/tested.
# On top of that, 00_application.conf is a *platform-managed* file that EB
# can regenerate on every deploy, so even a successful one-time edit by
# certbot isn't guaranteed to survive the next deploy.
#
# Instead:
#   1. Use certbot's `webroot` authenticator, which never touches nginx
#      config at all -- it just needs a directory nginx already serves.
#      We add that directory as a `location` INSIDE the existing
#      default_server block (via conf.d/elasticbeanstalk/, the same
#      mechanism 00_application.conf itself uses), so there's no
#      vhost-matching to get wrong: there's only one server block on
#      port 80, and this becomes part of it.
#   2. Write our OWN new "server { listen 443 ssl; ... }" block by hand
#      (via conf.d/, which nginx.conf includes at the http level, so a
#      new sibling server block is valid there), reusing the exact same
#      proxy_pass upstream that 00_application.conf uses -- extracted
#      with grep rather than hard-coded, so it can't drift from
#      whatever internal port this platform version actually uses.
set -e

DOMAIN="$(/opt/elasticbeanstalk/bin/get-config environment -k VOXBUDDY_HTTPS_DOMAIN 2>/dev/null || true)"
EMAIL="$(/opt/elasticbeanstalk/bin/get-config environment -k VOXBUDDY_HTTPS_EMAIL 2>/dev/null || true)"

if [ -z "$DOMAIN" ] || [ -z "$EMAIL" ]; then
  echo "VOXBUDDY_HTTPS_DOMAIN and/or VOXBUDDY_HTTPS_EMAIL are not set as " \
       "environment properties -- skipping Let's Encrypt setup. Set both " \
       "(DOMAIN = this environment's *.elasticbeanstalk.com hostname or " \
       "your custom domain, already pointed at it) and redeploy to enable " \
       "HTTPS." >&2
  exit 0
fi

APP_CONF="/etc/nginx/conf.d/elasticbeanstalk/00_application.conf"
if [ ! -f "$APP_CONF" ]; then
  echo "Expected $APP_CONF (EB-managed proxy config) not found -- " \
       "nginx layout may have changed on this platform version. " \
       "Skipping HTTPS setup rather than guessing." >&2
  exit 0
fi

# Reuse the exact upstream EB's own default_server block proxies to
# (e.g. "http://127.0.0.1:8000"), instead of hard-coding a port that
# could silently drift from what this platform version actually uses.
UPSTREAM="$(grep -oP 'proxy_pass\s+\K\S+(?=;)' "$APP_CONF" | head -n1)"
if [ -z "$UPSTREAM" ]; then
  echo "Could not find a proxy_pass target in $APP_CONF -- skipping " \
       "HTTPS setup rather than guessing the upstream port." >&2
  exit 0
fi

WEBROOT="/var/www/certbot"
sudo mkdir -p "$WEBROOT/.well-known/acme-challenge"

# Step 1: make sure the ACME HTTP-01 challenge path is servable over
# plain HTTP on this instance, as part of the one-and-only existing
# server block (default_server) -- no server_name matching involved.
sudo tee /etc/nginx/conf.d/elasticbeanstalk/01_acme_challenge.conf > /dev/null <<EOF
location /.well-known/acme-challenge/ {
    root $WEBROOT;
    try_files \$uri =404;
}
EOF

sudo nginx -t
sudo systemctl reload nginx

# Step 2: obtain/renew the certificate via webroot. certbot skips
# re-issuing if a valid cert already exists (renews near expiry
# instead), so re-running this every deploy is safe and doubles as
# your renewal mechanism as long as you redeploy at least once every
# ~60 days. (Let's Encrypt certs last 90 days; nothing here auto-renews
# on a timer -- add a cron/systemd-timer job later if the project
# outlives the hackathon and won't be redeployed that often.)
sudo certbot certonly -n --webroot -w "$WEBROOT" -d "$DOMAIN" \
  --agree-tos --email "$EMAIL" --keep-until-expiring

CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
if [ ! -f "$CERT_DIR/fullchain.pem" ] || [ ! -f "$CERT_DIR/privkey.pem" ]; then
  echo "Certbot did not produce a certificate at $CERT_DIR -- leaving " \
       "port 443 unconfigured this deploy. Check " \
       "/var/log/letsencrypt/letsencrypt.log on the instance." >&2
  exit 0
fi

# Step 3: write our own 443 server block by hand (not something certbot
# guessed at), proxying to the same upstream as the EB-managed 80
# block, including the WebSocket Upgrade/Connection headers the app's
# /ws/* endpoints need. $connection_upgrade is defined at the http
# level by EB's base nginx.conf, so it's visible here regardless of
# file split/ordering.
# No --redirect / forced HTTP->HTTPS here: plain HTTP on 80 (and the
# ACME challenge path above) keeps working too, so this doesn't
# interfere with any health check hitting the instance over HTTP.
sudo tee /etc/nginx/conf.d/02_https.conf > /dev/null <<EOF
server {
    listen 443 ssl;
    server_name $DOMAIN;

    ssl_certificate     $CERT_DIR/fullchain.pem;
    ssl_certificate_key $CERT_DIR/privkey.pem;

    location /.well-known/acme-challenge/ {
        root $WEBROOT;
        try_files \$uri =404;
    }

    location / {
        proxy_pass          $UPSTREAM;
        proxy_http_version  1.1;
        proxy_set_header    Connection          \$connection_upgrade;
        proxy_set_header    Upgrade             \$http_upgrade;
        proxy_set_header    Host                \$host;
        proxy_set_header    X-Real-IP           \$remote_addr;
        proxy_set_header    X-Forwarded-For     \$proxy_add_x_forwarded_for;
        proxy_set_header    X-Forwarded-Proto   https;
    }
}
EOF

sudo nginx -t
sudo systemctl reload nginx

echo "HTTPS configured for https://$DOMAIN (upstream: $UPSTREAM)."
