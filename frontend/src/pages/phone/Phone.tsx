import { SipPhone } from "@/features/sip/SipPhone";

// Второе рабочее место голосовой связи ДДС. Старший группы — по умолчанию;
// вышестоящего начальника (?role=superior) играет преподаватель: это
// обязательный маршрут по ответу заказчика (П.7).
const ROLES = {
  crew: { title: "Телефон старшего группы", extension: "6102", heading: "IP-телефон старшего группы" },
  superior: { title: "Телефон вышестоящего начальника", extension: "6103", heading: "IP-телефон вышестоящего начальника" },
} as const;

export function Phone() {
  const role = new URLSearchParams(window.location.search).get("role") === "superior" ? "superior" : "crew";
  const { title, extension, heading } = ROLES[role];
  return <main className="page phone-page">
    <h1>{heading}</h1>
    <p>Откройте этот экран на втором рабочем месте, зарегистрируйте номер {extension} и примите вызов ДДС 6101.</p>
    <SipPhone key={role} defaultExtension={extension} defaultTarget="6101" title={title} />
    <p className="ref">{role === "crew"
      ? <>Роль вышестоящего начальника — <a href="/phone?role=superior">телефон 6103</a>.</>
      : <>Роль старшего группы — <a href="/phone">телефон 6102</a>.</>}</p>
  </main>;
}
