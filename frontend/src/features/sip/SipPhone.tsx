import { useEffect, useRef, useState } from "react";

import { type SipStatus, useSipPhone } from "./useSipPhone";

const LABEL: Record<SipStatus, string> = {
  idle: "не подключён",
  connecting: "подключение…",
  registered: "зарегистрирован в Asterisk",
  incoming: "входящий вызов",
  calling: "вызов…",
  connected: "разговор идёт",
  error: "ошибка",
};

/** Абонент быстрого набора: пульт ДДС звонит бригаде и начальнику одной кнопкой. */
export interface SipContact {
  label: string;
  number: string;
}

export function SipPhone({
  defaultExtension = "6101", defaultTarget = "6102", title = "Локальный SIP/WebRTC", contacts = [],
}: {
  defaultExtension?: string;
  defaultTarget?: string;
  title?: string;
  contacts?: SipContact[];
}) {
  const phone = useSipPhone();
  const [extension, setExtension] = useState(defaultExtension);
  const [password, setPassword] = useState("");
  const [target, setTarget] = useState(defaultTarget);
  const audio = useRef<HTMLAudioElement | null>(null);
  const online = phone.status !== "idle" && phone.status !== "error";
  const inCall = phone.status === "calling" || phone.status === "incoming" || phone.status === "connected";

  useEffect(() => {
    if (audio.current) audio.current.srcObject = phone.remoteStream;
  }, [phone.remoteStream]);

  return <section className="sip-phone" aria-label={title}>
    <header><strong>{title}</strong><span className={phone.status === "registered" || phone.status === "connected" ? "state-ok" : "state-warn"}>● {LABEL[phone.status]}</span></header>
    <p className="sip-phone-note">Звук идёт напрямую через локальный Asterisk. Пароль хранится только в памяти этой вкладки.</p>
    {!online && <div className="sip-phone-settings">
      <label>Внутренний номер<input aria-label="SIP внутренний номер" inputMode="numeric" value={extension}
        onChange={(event) => setExtension(event.target.value)} /></label>
      <label>Пароль SIP<input aria-label="Пароль SIP" type="password" autoComplete="off" value={password}
        onChange={(event) => setPassword(event.target.value)} /></label>
      <button type="button" disabled={!password || phone.status === "connecting"}
        onClick={() => phone.connect({ extension, password })}>Подключить телефон</button>
    </div>}
    {online && <div className="sip-phone-controls">
      <label>Номер собеседника<input aria-label="Номер SIP собеседника" inputMode="numeric" value={target}
        disabled={inCall} onChange={(event) => setTarget(event.target.value)} /></label>
      {phone.status === "registered" && <button type="button" onClick={() => phone.dial(target)}>Позвонить</button>}
      {phone.status === "registered" && contacts.map((contact) => <button type="button" key={contact.number}
        onClick={() => { setTarget(contact.number); phone.dial(contact.number); }}>
        Позвонить: {contact.label} · {contact.number}
      </button>)}
      {phone.status === "incoming" && <button type="button" className="sip-answer" onClick={phone.answer}>Ответить · {phone.peer}</button>}
      {inCall && phone.status !== "incoming" && <span>{phone.peer || target}</span>}
      {inCall && <button type="button" className="sip-hangup" onClick={phone.hangup}>Завершить</button>}
      {!inCall && <button type="button" onClick={phone.disconnect}>Отключить</button>}
    </div>}
    {phone.error && <p className="state-violated" role="alert">{phone.error}</p>}
    <audio ref={audio} autoPlay playsInline aria-label="Звук SIP-собеседника" />
  </section>;
}
