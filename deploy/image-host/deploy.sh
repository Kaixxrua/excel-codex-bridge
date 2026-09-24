#!/usr/bin/env bash
# Deploy the excel-codex image host with Docker, in one command.
#
#   deploy/image-host/deploy.sh img.example.com                  # your own image host
#   deploy/image-host/deploy.sh img.example.com --relay          # an open relay (anyone may upload)
#   deploy/image-host/deploy.sh img.example.com --behind-proxy   # you already run a web server on 80/443
#
# Point the domain's DNS at this server first. Without --behind-proxy, Caddy
# takes ports 80 and 443 and gets the https certificate by itself. Settings
# live in deploy/image-host/.env; run this again after editing it or after a
# git pull to rebuild and restart.
set -euo pipefail

usage() {
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

domain=""
relay=0
behind_proxy=0
port=8095
while [ $# -gt 0 ]; do
    case "$1" in
        --relay) relay=1 ;;
        --behind-proxy) behind_proxy=1 ;;
        --port) port="${2:?--port needs a number}"; shift ;;
        -h|--help) usage ;;
        -*) echo "Unknown option: $1" >&2; usage ;;
        *) [ -z "$domain" ] || usage; domain="$1" ;;
    esac
    shift
done
[ -n "$domain" ] || usage
case "$domain" in
    http://*|https://*|*/*) echo "Give the bare domain, e.g. img.example.com" >&2; exit 2 ;;
esac

cd "$(dirname "$0")"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
    echo "This needs Docker with the compose plugin: https://docs.docker.com/engine/install/" >&2
    exit 1
fi

setting() { sed -n "s/^$1=//p" .env | tail -n 1; }

if [ -f .env ]; then
    echo "Keeping the settings in $(pwd)/.env"
    if [ "$(setting IMAGE_HOST_DOMAIN)" != "$domain" ]; then
        echo "  Note: it is set up for $(setting IMAGE_HOST_DOMAIN), not $domain; edit or delete .env to change that." >&2
    fi
else
    (
    umask 077
    {
        echo "IMAGE_HOST_DOMAIN=$domain"
        echo "IMAGE_HOST_PUBLIC_URL=https://$domain"
        echo "IMAGE_HOST_PORT=$port"
        if [ "$relay" = 1 ]; then
            echo "# An open relay: no token; per-address quotas, pictures saved afresh, served only to OpenAI."
            echo "IMAGE_HOST_OPEN=1"
            echo "IMAGE_HOST_TTL_HOURS=1"
            echo "#IMAGE_HOST_UPLOADS_PER_HOUR=240"
            echo "#IMAGE_HOST_UPLOAD_MB_PER_DAY=300"
        else
            token="$(head -c 24 /dev/urandom | base64 | tr -d '/+=\n')"
            echo "# Upload tokens, comma-separated: give one to each bridge that uses this host."
            echo "IMAGE_HOST_TOKENS=$token"
            echo "IMAGE_HOST_TTL_HOURS=24"
        fi
        echo "#IMAGE_HOST_MAX_MB=10"
        echo "#IMAGE_HOST_MAX_TOTAL_MB=1024"
    } > .env
    )
    echo "Wrote the settings to $(pwd)/.env"
fi

profiles=()
[ "$behind_proxy" = 1 ] || profiles=(--profile caddy)
docker compose ${profiles[@]+"${profiles[@]}"} up -d --build

printf "Waiting for the image host"
container="$(docker compose ps -q image-host)"
for _ in $(seq 1 60); do
    status="$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || true)"
    [ "$status" = healthy ] && break
    printf "."
    sleep 1
done
echo
if [ "$status" != healthy ]; then
    echo "The image host did not come up; see: docker compose -f $(pwd)/docker-compose.yml logs image-host" >&2
    exit 1
fi

listen="127.0.0.1:$(setting IMAGE_HOST_PORT)"
url="$(setting IMAGE_HOST_PUBLIC_URL)"
echo "The image host runs on $listen."
if [ "$behind_proxy" = 1 ]; then
    cat <<EOF
Send https://$domain to it from your web server, replacing any X-Forwarded-For
the client sent (the quotas go by it). For Caddy:

    $domain {
        reverse_proxy $listen
    }

For nginx, inside the server block for $domain:

    client_max_body_size 12m;
    location / {
        proxy_pass http://$listen;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-For \$remote_addr;
    }
EOF
else
    echo "Caddy serves it at $url (the certificate comes on the first visit)."
fi
echo
if [ -n "$(setting IMAGE_HOST_TOKENS)" ]; then
    echo "On each computer that runs the bridge:"
    echo "    excel-codex image-host set $url $(setting IMAGE_HOST_TOKENS | cut -d, -f1)"
else
    echo "This is an open relay. Bridges use it with:"
    echo "    EXCEL_BRIDGE_IMAGE_HOST=relay EXCEL_BRIDGE_RELAY_URL=$url excel-codex"
fi
