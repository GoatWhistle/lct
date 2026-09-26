import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { transform } from "esbuild";

const source = await readFile(new URL("../src/shared/api/ws.ts", import.meta.url), "utf8");
const { code } = await transform(source, { loader: "ts", format: "esm" });
const moduleUrl = `data:text/javascript;base64,${Buffer.from(code).toString("base64")}`;
const { callChannel, stationChannel } = await import(moduleUrl);
const mergeSource = await readFile(new URL("../src/features/kio-card/merge.ts", import.meta.url), "utf8");
const { code: mergeCode } = await transform(mergeSource, { loader: "ts", format: "esm" });
const { applyPatch, applyState, edit, empty } = await import(
  `data:text/javascript;base64,${Buffer.from(mergeCode).toString("base64")}`,
);

class FakeWebSocket {
  static OPEN = 1;
  static instances = [];
  readyState = 0;
  sent = [];

  constructor(url) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(value) { this.sent.push(JSON.parse(value)); }
  open() { this.readyState = FakeWebSocket.OPEN; this.onopen(); }
  drop() { this.readyState = 3; this.onclose(); }
  receive(event) { this.onmessage({ data: JSON.stringify(event) }); }
}

globalThis.WebSocket = FakeWebSocket;
globalThis.location = { protocol: "http:", host: "localhost" };
const reconnectTimers = [];
globalThis.setTimeout = (callback) => (reconnectTimers.push(callback), reconnectTimers.length);
globalThis.clearTimeout = () => {};

function reconnect(socket) {
  socket.drop();
  reconnectTimers.shift()();
  const next = FakeWebSocket.instances.at(-1);
  next.open();
  return next;
}

const call = callChannel("session-1").connect();
let socket = FakeWebSocket.instances.at(-1);
socket.open();
const patch = {
  type: "kio.patch",
  fields: {
    address: "ул. Тестовая, 1",
    notify: ["101", "СМП"],
    coords: { latitude: 55.7, longitude: 37.6 },
  },
};
assert.equal(call.send(patch), true);
const sentPatch = socket.sent[0];
assert.equal(sentPatch.type, patch.type);
assert.deepEqual(sentPatch.fields, patch.fields);
assert.equal(typeof sentPatch._command_id, "string");
socket = reconnect(socket);
assert.deepEqual(socket.sent, [sentPatch], "unacknowledged KIO patch is replayed after reconnect");
socket.receive({ type: "kio.patch", fields: patch.fields, source: "auto" });
socket = reconnect(socket);
assert.deepEqual(socket.sent, [sentPatch], "unrelated automatic patch does not acknowledge operator input");
socket.receive({ type: "kio.patch", fields: { address: "ул. Другая, 2" }, source: "operator" });
socket = reconnect(socket);
assert.deepEqual(socket.sent, [sentPatch], "different operator state does not acknowledge the pending patch");
socket.receive({ type: "kio.patch", fields: {
  address: patch.fields.address,
  notify: ["101", "СМП"],
  coords: { longitude: 37.6, latitude: 55.7 },
}, source: "operator" });
socket = reconnect(socket);
assert.deepEqual(socket.sent, [], "matching checkpoint echo clears the KIO patch");
const edited = edit(empty, "coords", { latitude: 55.7, longitude: 37.6 });
assert.deepEqual(applyPatch(edited, { coords: { longitude: 37.6, latitude: 55.7 } }, "operator").pending, {});
assert.deepEqual(applyState(edited, { coords: { longitude: 37.6, latitude: 55.7 } }).pending, {});

const dds = stationChannel("session-1", "dds").connect();
socket = FakeWebSocket.instances.at(-1);
socket.open();
const crewSelect = { type: "crew.select", crew: "ПСЧ-1" };
assert.equal(dds.send(crewSelect), true);
const sentCrewSelect = socket.sent[0];
assert.equal(sentCrewSelect.type, crewSelect.type);
assert.equal(sentCrewSelect.crew, crewSelect.crew);
assert.equal(typeof sentCrewSelect._command_id, "string");
socket = reconnect(socket);
assert.deepEqual(socket.sent, [sentCrewSelect], "station command is replayed with its stable ID");
socket.receive({ type: "command.ack", command_id: sentCrewSelect._command_id });
socket.sent = [];

const reply = { type: "card.reply", card_id: "card-1", text: "Принято" };
assert.equal(dds.send(reply), true);
const sentReply = socket.sent[0];
assert.equal(sentReply.type, reply.type);
assert.equal(sentReply.card_id, reply.card_id);
assert.equal(sentReply.text, reply.text);
socket = reconnect(socket);
assert.deepEqual(socket.sent, [sentReply], "unacknowledged reply is replayed after reconnect");
socket.receive({ type: "station.state", snapshot: { card_id: "card-2", reply_text: "Принято" } });
socket = reconnect(socket);
assert.deepEqual(socket.sent, [sentReply], "reply for another card does not acknowledge this card");
socket.receive({ type: "station.state", snapshot: { card_id: "card-1", reply_text: "Принято" } });
socket = reconnect(socket);
assert.deepEqual(socket.sent, [], "matching persisted station snapshot clears the reply");

console.log("WebSocket durable outbox replay/ack tests passed");
