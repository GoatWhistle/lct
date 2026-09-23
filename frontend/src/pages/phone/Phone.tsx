import { SipPhone } from "@/features/sip/SipPhone";

export function Phone() {
  return <main className="page phone-page">
    <h1>IP-телефон старшего группы</h1>
    <p>Откройте этот экран на втором рабочем месте, зарегистрируйте номер 6102 и примите вызов ДДС 6101.</p>
    <SipPhone defaultExtension="6102" defaultTarget="6101" title="Телефон старшего группы" />
  </main>;
}
