// session_id живёт в URL, а не в сообщении: преподаватель открывает
// /instructor?session=..., монитор — /wall?session=..., без логина,
// со второй машины по ссылке (docs/arch/CONTRACT.md).

export function sessionIdFromUrl(): string | null {
  return new URLSearchParams(location.search).get("session");
}
