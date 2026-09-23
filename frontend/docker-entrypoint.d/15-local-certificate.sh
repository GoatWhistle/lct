#!/bin/sh
set -eu

if [ "${TLS_BOOTSTRAP:-false}" != "true" ]; then
    exit 0
fi

certificate_dir=/etc/nginx/tls
certificate_file="$certificate_dir/tls.crt"
private_key_file="$certificate_dir/tls.key"
mkdir -p "$certificate_dir"

if [ -s "$certificate_file" ] && [ -s "$private_key_file" ]; then
    exit 0
fi

openssl req -x509 -nodes -newkey rsa:3072 -sha256 -days 365 \
    -keyout "$private_key_file" \
    -out "$certificate_file" \
    -subj "/CN=localhost/O=LCT local training stand" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" 2>/dev/null
chmod 600 "$private_key_file"
chmod 644 "$certificate_file"
echo "TLS: создан локальный bootstrap-сертификат $certificate_file" >&2
