// Express middleware that runs Shield's rules on JSON chat bodies.
//
// It reads the GENERATED shield-rules.js (a copy of
// src/byoai/browser_extension/shield-rules.js, made by `npm run sync-rules`),
// so it mirrors the Python rules in byoai.integrations.shield; the
// `rules_version` in that file names the exact set. Re-run sync-rules after
// upgrading byoai-runtime. Two things differ from Python: a private key is
// blocked on its header (never rewritten), and files are not scanned here.
//
// secret rules -> 403 (coriqo_shield_blocked); credential_assign warns in the
// browser and blocks here (nobody to ask); pii rules -> placeholders.
const path = require('path')
global.window = global.window || global // shield-rules.js registers itself on `window`
require(path.join(__dirname, 'shield-rules.js'))
const R = global.__shieldRules

const luhn = (s) => {
  const d = s.replace(/\D/g, '')
  if (d.length < 12) return false
  let sum = 0, alt = false
  for (let i = d.length - 1; i >= 0; i--) {
    let n = +d[i]
    if (alt && (n *= 2) > 9) n -= 9
    sum += n; alt = !alt
  }
  return sum % 10 === 0
}
const iban = (s) => {
  const t = s.replace(/\s/g, '').toUpperCase()
  const r = t.slice(4) + t.slice(0, 4)
  let rem = 0
  for (const ch of r) for (const c of String(parseInt(ch, 36))) rem = (rem * 10 + +c) % 97
  return rem === 1
}
const validators = { luhn, iban }
const ok = (rule, v) => !R.validators[rule] || validators[R.validators[rule]](v)
const compile = (tier) => R[tier].map(([name, src, fl]) => [name, new RegExp(src, fl + 'g')])
const RULES = { secret: compile('secret'), pii: compile('pii') }

// Every string in the body except id-like keys and base64 payloads gets the full
// treatment (secrets block, personal details become placeholders).
const idish = (k) => /^(id|uuid|model|signature|media_type|mimetype|mime_type)$/i.test(k) || /_(id|uuid)$/i.test(k)
function walk(node, fix) {
  if (typeof node === 'string') return fix(node)
  if (Array.isArray(node)) return node.map((x) => walk(x, fix))
  if (node && typeof node === 'object') {
    const bin = node.type === 'base64' || 'mimeType' in node || 'mime_type' in node
    for (const k of Object.keys(node)) {
      if (idish(k) || (bin && k.toLowerCase() === 'data')) continue
      node[k] = walk(node[k], fix)
    }
  }
  return node
}

// ...and the secret rules also run over EVERY string, key and base64 data field,
// so a key under `id`, `metadata.user_id` or a document `data` cannot slip past.
function b64text(s, key) {
  const i = s.slice(0, 200).indexOf('base64,')
  if (i >= 0) s = s.slice(i + 7)
  else if (key !== 'data' && key !== 'file_data') return null
  if (s.length < 16 || s.length > 7e6 || !/^[A-Za-z0-9+/\-_\s]+={0,2}$/.test(s)) return null
  try {
    const buf = Buffer.from(s.replace(/-/g, '+').replace(/_/g, '/'), 'base64')
    return new TextDecoder('utf-8', { fatal: true }).decode(buf)
  } catch { return null }
}
function sweep(node, hit, key = '') {
  const scan = (t) => {
    for (const [name, re] of RULES.secret) {
      re.lastIndex = 0
      for (let m; (m = re.exec(t));) if (ok(name, m[0])) hit.add(name)
    }
  }
  if (typeof node === 'string') {
    scan(node)
    const inner = b64text(node, key)
    if (inner !== null) scan(inner)
  } else if (Array.isArray(node)) node.forEach((x) => sweep(x, hit, key))
  else if (node && typeof node === 'object') {
    for (const k of Object.keys(node)) { scan(k); sweep(node[k], hit, k.toLowerCase()) }
  }
}

function check(body) {
  const blocked = new Set(), redacted = new Set(), seen = {}
  const fix = (text) => {
    for (const [name, re] of RULES.pii) {
      const label = R.placeholder[name]
      text = text.replace(re, (v) => {
        if (!label || !ok(name, v)) return v
        redacted.add(name)
        const s = (seen[label] = seen[label] || new Map())
        if (!s.has(v)) s.set(v, s.size + 1)
        return `[${label}_${s.get(v)}]`
      })
    }
    return text
  }
  const out = walk(JSON.parse(JSON.stringify(body)), fix)
  sweep(body, blocked)
  return { blocked: [...blocked], redacted: [...redacted], body: out }
}

const norm = (p) => '/' + p.replace(/^\/+|\/+$/g, '').toLowerCase()

function shieldGuard({ paths = ['/v1/chat/completions', '/v1/messages', '/v1/responses'], mode = 'enforce' } = {}) {
  const wanted = paths.map(norm)
  const stop = (req, res, blocked) => {
    const message = `Shield stopped this request: it contains ${blocked.join(', ')}.`
    const err = norm(req.path).endsWith('/messages')
      ? { type: 'error', error: { type: 'coriqo_shield_blocked', message, rules: blocked } }
      : { error: { message, type: 'coriqo_shield_blocked', code: 'shield_blocked', rules: blocked } }
    return res.status(403).json(err)
  }
  // Works with express.json() (req.body is an object) and with express.text({type: '*/*'})
  // (req.body is a string: JSON is parsed here, anything else is scanned as text for secrets).
  return (req, res, next) => {
    if (!['POST', 'PUT', 'PATCH'].includes(req.method) || !wanted.includes(norm(req.path))) return next()
    let body = req.body
    if (typeof body === 'string') {
      const hit = new Set()
      sweep(body, hit) // the raw text too: repeated keys, anything JSON.parse would drop
      try { body = JSON.parse(body) } catch { body = null }
      if (body === null || typeof body !== 'object') {
        if (mode === 'enforce' && hit.size) return stop(req, res, [...hit])
        return next()
      }
      if (mode === 'enforce' && hit.size) return stop(req, res, [...hit])
    }
    if (!body || typeof body !== 'object') return next()
    const d = check(body)
    if (mode === 'enforce' && d.blocked.length) return stop(req, res, d.blocked)
    req.body = mode === 'enforce' && d.redacted.length ? d.body : body
    req.shield = { rules: [...d.blocked, ...d.redacted], rules_version: R.rules_version }
    next()
  }
}

module.exports = { shieldGuard, check, norm }
