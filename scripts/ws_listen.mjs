// Connects to the WebSocket as one PWA user and prints every pushed message.
// Usage: node scripts/ws_listen.mjs <wss-url> [GRANDMA_PWA_TOKEN|JIMIN_PWA_TOKEN] ['<json to send after sync>']
// Sends {"action":"sync"} on connect. The token is read from .env.tokens, never from the command line.
import { readFileSync } from "node:fs";

const [webSocketUrl, tokenVariable = "GRANDMA_PWA_TOKEN", messageAfterSync] = process.argv.slice(2);
const tokenLine = readFileSync(new URL("../.env.tokens", import.meta.url), "utf8")
  .split("\n")
  .find((line) => line.startsWith(`${tokenVariable}=`));
if (!webSocketUrl || !tokenLine) {
  console.error("Need a wss:// URL and a seeded .env.tokens (run scripts/seed_graph.py)");
  process.exit(1);
}
const accessToken = tokenLine.slice(tokenVariable.length + 1);
const socket = new WebSocket(`${webSocketUrl}?token=${encodeURIComponent(accessToken)}`);
socket.addEventListener("open", () => {
  console.log(`connected as ${tokenVariable}`);
  socket.send(JSON.stringify({ action: "sync" }));
  if (messageAfterSync) setTimeout(() => socket.send(messageAfterSync), 1000);
});
socket.addEventListener("message", (event) => console.log(event.data));
socket.addEventListener("close", (event) => console.log(`closed ${event.code}`));
socket.addEventListener("error", () => console.error("connection error"));
