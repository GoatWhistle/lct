#!/bin/sh
set -eu

credentials=/var/lib/lct-sip/credentials.env
mkdir -p /var/lib/lct-sip /recordings
umask 077

override_6001=${SIP_6001_PASSWORD:-}
override_6002=${SIP_6002_PASSWORD:-}
override_6003=${SIP_6003_PASSWORD:-}
override_6101=${SIP_6101_PASSWORD:-}
override_6102=${SIP_6102_PASSWORD:-}
override_6103=${SIP_6103_PASSWORD:-}
if [ -s "$credentials" ]; then
    # Файл содержит только сгенерированные hex-значения без shell-конструкций.
    . "$credentials"
fi
SIP_6001_PASSWORD=${override_6001:-${SIP_6001_PASSWORD:-$(openssl rand -hex 16)}}
SIP_6002_PASSWORD=${override_6002:-${SIP_6002_PASSWORD:-$(openssl rand -hex 16)}}
SIP_6003_PASSWORD=${override_6003:-${SIP_6003_PASSWORD:-$(openssl rand -hex 16)}}
SIP_6101_PASSWORD=${override_6101:-${SIP_6101_PASSWORD:-$(openssl rand -hex 16)}}
SIP_6102_PASSWORD=${override_6102:-${SIP_6102_PASSWORD:-$(openssl rand -hex 16)}}
SIP_6103_PASSWORD=${override_6103:-${SIP_6103_PASSWORD:-$(openssl rand -hex 16)}}
SIP_EXTERNAL_MEDIA_ADDRESS=${SIP_EXTERNAL_MEDIA_ADDRESS:-127.0.0.1}
SIP_RTP_START=${SIP_RTP_START:-10000}
SIP_RTP_END=${SIP_RTP_END:-10099}
case "$SIP_RTP_START:$SIP_RTP_END" in
    *[!0-9:]*|:*|*:) echo "SIP_RTP_START/END must be positive decimal ports" >&2; exit 64 ;;
esac
if [ "$SIP_RTP_START" -lt 1024 ] || [ "$SIP_RTP_END" -gt 65535 ] \
    || [ "$SIP_RTP_END" -lt "$SIP_RTP_START" ]; then
    echo "SIP RTP port range must satisfy 1024 <= START <= END <= 65535" >&2
    exit 64
fi
cat > "$credentials" <<EOF
SIP_6001_PASSWORD=$SIP_6001_PASSWORD
SIP_6002_PASSWORD=$SIP_6002_PASSWORD
SIP_6003_PASSWORD=$SIP_6003_PASSWORD
SIP_6101_PASSWORD=$SIP_6101_PASSWORD
SIP_6102_PASSWORD=$SIP_6102_PASSWORD
SIP_6103_PASSWORD=$SIP_6103_PASSWORD
EOF
chmod 600 "$credentials"

export SIP_6001_PASSWORD SIP_6002_PASSWORD SIP_6003_PASSWORD SIP_6101_PASSWORD SIP_6102_PASSWORD SIP_6103_PASSWORD SIP_EXTERNAL_MEDIA_ADDRESS
export SIP_RTP_START SIP_RTP_END
envsubst '${SIP_6001_PASSWORD} ${SIP_6002_PASSWORD} ${SIP_6003_PASSWORD} ${SIP_6101_PASSWORD} ${SIP_6102_PASSWORD} ${SIP_6103_PASSWORD} ${SIP_EXTERNAL_MEDIA_ADDRESS}' \
    < /opt/lct-sip/pjsip.conf.template > /etc/asterisk/pjsip.conf
chmod 640 /etc/asterisk/pjsip.conf
chown root:asterisk /etc/asterisk/pjsip.conf

envsubst '${SIP_RTP_START} ${SIP_RTP_END}' \
    < /opt/lct-sip/rtp.conf.template > /etc/asterisk/rtp.conf
chmod 640 /etc/asterisk/rtp.conf
chown root:asterisk /etc/asterisk/rtp.conf

chown -R asterisk:asterisk /recordings /var/lib/lct-sip

exec asterisk -f -U asterisk -G asterisk -vvv
