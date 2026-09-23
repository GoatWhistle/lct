#!/bin/sh
set -eu

credentials=/var/lib/lct-sip/credentials.env
mkdir -p /var/lib/lct-sip /recordings /tls
umask 077

override_6001=${SIP_6001_PASSWORD:-}
override_6002=${SIP_6002_PASSWORD:-}
override_6003=${SIP_6003_PASSWORD:-}
override_6101=${SIP_6101_PASSWORD:-}
override_6102=${SIP_6102_PASSWORD:-}
if [ -s "$credentials" ]; then
    # Файл содержит только сгенерированные hex-значения без shell-конструкций.
    . "$credentials"
fi
SIP_6001_PASSWORD=${override_6001:-${SIP_6001_PASSWORD:-$(openssl rand -hex 16)}}
SIP_6002_PASSWORD=${override_6002:-${SIP_6002_PASSWORD:-$(openssl rand -hex 16)}}
SIP_6003_PASSWORD=${override_6003:-${SIP_6003_PASSWORD:-$(openssl rand -hex 16)}}
SIP_6101_PASSWORD=${override_6101:-${SIP_6101_PASSWORD:-$(openssl rand -hex 16)}}
SIP_6102_PASSWORD=${override_6102:-${SIP_6102_PASSWORD:-$(openssl rand -hex 16)}}
SIP_EXTERNAL_MEDIA_ADDRESS=${SIP_EXTERNAL_MEDIA_ADDRESS:-127.0.0.1}
cat > "$credentials" <<EOF
SIP_6001_PASSWORD=$SIP_6001_PASSWORD
SIP_6002_PASSWORD=$SIP_6002_PASSWORD
SIP_6003_PASSWORD=$SIP_6003_PASSWORD
SIP_6101_PASSWORD=$SIP_6101_PASSWORD
SIP_6102_PASSWORD=$SIP_6102_PASSWORD
EOF
chmod 600 "$credentials"

export SIP_6001_PASSWORD SIP_6002_PASSWORD SIP_6003_PASSWORD SIP_6101_PASSWORD SIP_6102_PASSWORD SIP_EXTERNAL_MEDIA_ADDRESS
envsubst '${SIP_6001_PASSWORD} ${SIP_6002_PASSWORD} ${SIP_6003_PASSWORD} ${SIP_6101_PASSWORD} ${SIP_6102_PASSWORD} ${SIP_EXTERNAL_MEDIA_ADDRESS}' \
    < /opt/lct-sip/pjsip.conf.template > /etc/asterisk/pjsip.conf
chmod 640 /etc/asterisk/pjsip.conf
chown root:asterisk /etc/asterisk/pjsip.conf

if [ ! -s /tls/sip.crt ] || [ ! -s /tls/sip.key ]; then
    openssl req -x509 -nodes -newkey rsa:3072 -sha256 -days 365 \
        -keyout /tls/sip.key -out /tls/sip.crt \
        -subj '/CN=localhost/O=LCT local SIP training stand' \
        -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
        -addext 'extendedKeyUsage=serverAuth' 2>/dev/null
fi
chmod 640 /tls/sip.key
chmod 644 /tls/sip.crt
chown root:asterisk /tls/sip.key /tls/sip.crt
chown -R asterisk:asterisk /recordings /var/lib/lct-sip

exec asterisk -f -U asterisk -G asterisk -vvv
