import { useCallback, useEffect, useRef, useState } from "react";
import { UA, WebSocketInterface } from "jssip";
import type { RTCSession } from "jssip/lib/RTCSession";
import type { RTCSessionEvent } from "jssip/lib/UA";

export type SipStatus =
  | "idle" | "connecting" | "registered" | "incoming" | "calling" | "connected" | "error";

export interface SipCredentials {
  extension: string;
  password: string;
  domain?: string;
}

const media = { audio: true, video: false } satisfies MediaStreamConstraints;

export function useSipPhone() {
  const uaRef = useRef<UA | null>(null);
  const sessionRef = useRef<RTCSession | null>(null);
  const registeredRef = useRef(false);
  const domainRef = useRef(location.hostname);
  const [status, setStatus] = useState<SipStatus>("idle");
  const [error, setError] = useState("");
  const [peer, setPeer] = useState("");
  const [remoteStream, setRemoteStream] = useState<MediaStream | null>(null);

  const clearSession = useCallback(() => {
    sessionRef.current = null;
    setPeer("");
    setRemoteStream(null);
    setStatus(registeredRef.current ? "registered" : "idle");
  }, []);

  const attachSession = useCallback((session: RTCSession, incoming: boolean) => {
    if (sessionRef.current === session) return;
    if (sessionRef.current && !sessionRef.current.isEnded()) {
      session.terminate({ status_code: 486, reason_phrase: "Busy Here" });
      return;
    }
    sessionRef.current = session;
    setError("");
    setPeer(session.remote_identity?.uri?.user || "абонент");
    setStatus(incoming ? "incoming" : "calling");
    const observedConnections = new WeakSet<RTCPeerConnection>();
    const observeConnection = () => {
      const connection = session.connection;
      if (!connection || observedConnections.has(connection)) return;
      observedConnections.add(connection);
      connection.addEventListener("track", (event) => {
        const stream = event.streams[0] ?? new MediaStream([event.track]);
        setRemoteStream(stream);
      });
    };
    observeConnection();
    session.on("peerconnection", observeConnection);
    session.on("accepted", () => setStatus("connected"));
    session.on("confirmed", () => setStatus("connected"));
    session.on("ended", clearSession);
    session.on("failed", (event) => {
      setError(`Вызов завершён: ${event.cause}`);
      clearSession();
    });
  }, [clearSession]);

  const disconnect = useCallback(() => {
    const session = sessionRef.current;
    if (session && !session.isEnded()) session.terminate();
    sessionRef.current = null;
    registeredRef.current = false;
    uaRef.current?.stop();
    uaRef.current = null;
    setRemoteStream(null);
    setPeer("");
    setError("");
    setStatus("idle");
  }, []);

  const connect = useCallback((credentials: SipCredentials) => {
    const extension = credentials.extension.trim();
    const password = credentials.password;
    const domain = credentials.domain?.trim() || location.hostname;
    if (!/^\d{4}$/.test(extension) || !password) {
      setError("Укажите четырёхзначный внутренний номер и пароль SIP.");
      return false;
    }
    disconnect();
    domainRef.current = domain;
    setStatus("connecting");
    try {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      const socket = new WebSocketInterface(`${scheme}://${location.host}/sip-ws`);
      const ua = new UA({
        sockets: socket,
        uri: `sip:${extension}@${domain}`,
        authorization_user: extension,
        password,
        register: true,
        register_expires: 300,
        connection_recovery_min_interval: 2,
        connection_recovery_max_interval: 8,
        user_agent: "LCT-112-WebRTC/1.0",
      });
      ua.on("registered", () => {
        registeredRef.current = true;
        setError("");
        setStatus("registered");
      });
      ua.on("registrationFailed", (event) => {
        registeredRef.current = false;
        setError(`Регистрация SIP отклонена: ${event.cause || "проверьте номер и пароль"}`);
        setStatus("error");
      });
      ua.on("disconnected", () => {
        registeredRef.current = false;
        if (!uaRef.current) return;
        setStatus("connecting");
      });
      ua.on("newRTCSession", (event: RTCSessionEvent) => {
        attachSession(event.session, event.originator === "remote");
      });
      uaRef.current = ua;
      ua.start();
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setStatus("error");
      return false;
    }
  }, [attachSession, disconnect]);

  const dial = useCallback((target: string) => {
    const normalized = target.trim();
    if (!uaRef.current || !registeredRef.current || !/^\d{4}$/.test(normalized)) {
      setError("Сначала зарегистрируйте телефон и укажите номер из четырёх цифр.");
      return false;
    }
    try {
      const session = uaRef.current.call(`sip:${normalized}@${domainRef.current}`, {
        mediaConstraints: media,
      });
      attachSession(session, false);
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      setStatus("error");
      return false;
    }
  }, [attachSession]);

  const answer = useCallback(() => {
    const session = sessionRef.current;
    if (!session || status !== "incoming") return false;
    try {
      session.answer({ mediaConstraints: media });
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      return false;
    }
  }, [status]);

  const hangup = useCallback(() => {
    const session = sessionRef.current;
    if (!session || session.isEnded()) return;
    session.terminate();
  }, []);

  useEffect(() => disconnect, [disconnect]);

  return { status, error, peer, remoteStream, connect, disconnect, dial, answer, hangup };
}
