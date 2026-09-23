FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends asterisk ca-certificates gettext-base openssl \
    && rm -rf /var/lib/apt/lists/*

COPY pjsip.conf.template /opt/lct-sip/pjsip.conf.template
COPY extensions.conf /etc/asterisk/extensions.conf
COPY http.conf /etc/asterisk/http.conf
COPY modules.conf /etc/asterisk/modules.conf
COPY rtp.conf /etc/asterisk/rtp.conf
COPY entrypoint.sh /usr/local/bin/lct-sip-entrypoint
RUN chmod +x /usr/local/bin/lct-sip-entrypoint \
    && mkdir -p /recordings /var/lib/lct-sip /tls \
    && chown -R asterisk:asterisk /recordings /var/lib/lct-sip

EXPOSE 5060/udp 5060/tcp 5061/tcp 8088/tcp 10000-10099/udp
HEALTHCHECK --interval=10s --timeout=3s --retries=6 --start-period=15s \
    CMD asterisk -rx "core show uptime" >/dev/null 2>&1 || exit 1

ENTRYPOINT ["/usr/local/bin/lct-sip-entrypoint"]
