/**
 * Test stand-ins for the relay's MessageChannel: synchronous port pairs, and
 * the single "here is your port" message the relay posts to the page.
 */
export function fakeChannel() {
  const mk = () => ({ onmessage: null, peer: null, close() {}, postMessage(d) { this.peer.onmessage?.({ data: structuredClone(d) }) } })
  const port1 = mk()
  const port2 = mk()
  port1.peer = port2
  port2.peer = port1
  return { port1, port2 }
}

/** Deliver `port` to the page the way the relay does. Returns the event, for hiding checks. */
export function postPort(win, EventCtor, port, mark = 'shield-agent-port') {
  const ev = new EventCtor('message', { cancelable: true })
  Object.defineProperties(ev, { data: { value: mark }, source: { value: win }, ports: { value: [port] } })
  win.dispatchEvent(ev)
  return ev
}

/** Act as the relay for a page without one: open the channel, send `config`. Returns the relay's end. */
export function connectPage(win, EventCtor, config) {
  const ch = fakeChannel()
  postPort(win, EventCtor, ch.port2)
  if (config) ch.port1.postMessage({ t: 'config', ...config })
  return ch.port1
}
