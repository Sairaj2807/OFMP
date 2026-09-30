#!/usr/bin/env sh
# Self-signed certificate for LOCAL or STAGING testing of the nginx container.
# Production uses Let's Encrypt (see docs/deployment.md).
#   deploy/scripts/selfsigned-cert.sh [hostname] [cert-dir]
set -eu
HOST="${1:-localhost}"
DIR="${2:-deploy/certs}"
mkdir -p "$DIR"
docker run --rm -v "$(cd "$DIR" && pwd):/out" alpine/openssl req -x509 -nodes -newkey rsa:2048 -days 30 \
  -keyout /out/privkey.pem -out /out/fullchain.pem \
  -subj "/CN=${HOST}" -addext "subjectAltName=DNS:${HOST},DNS:localhost,IP:127.0.0.1"
echo "wrote $DIR/fullchain.pem and $DIR/privkey.pem (self-signed, 30 days, CN=$HOST)"
