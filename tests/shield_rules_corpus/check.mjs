// Precision gate for the browser's copy of the rules: 0 block/warn hits on
// benign.txt, >= 95% recall on positive.txt. The Python side asserts the same
// thresholds on the same files (tests/integrations/test_shield_rules_v2.py).
//
//   node tests/shield_rules_corpus/check.mjs
import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const rulesFile = path.join(here, '..', '..', 'src', 'byoai', 'browser_extension', 'shield-rules.js')
const win = {}
vm.runInNewContext(fs.readFileSync(rulesFile, 'utf8'), { window: win })
const R = win.__shieldRules

function luhn(s) {
  const d = s.replace(/\D/g, '')
  if (d.length < 13 || d.length > 19) return false
  let total = 0
  for (let i = 0; i < d.length; i++) {
    let x = Number(d[d.length - 1 - i])
    if (i % 2 === 1) { x *= 2; if (x > 9) x -= 9 }
    total += x
  }
  return total % 10 === 0
}
function iban(s) {
  const v = s.replace(/ /g, '')
  if (v.length < 15 || v.length > 34) return false
  const moved = v.slice(4) + v.slice(0, 4)
  let rem = 0
  for (const ch of moved) {
    const n = parseInt(ch, 36)
    for (const digit of String(n)) rem = (rem * 10 + Number(digit)) % 97
  }
  return rem === 1
}
const VALIDATORS = { luhn, iban }

const ORDER = ['log', 'redact', 'warn', 'block']
const stricter = (a, b) => (ORDER.indexOf(a) >= ORDER.indexOf(b) ? a : b)

function hits(text) {
  const out = []
  for (const tier of ['secret', 'pii', 'flag']) {
    for (const [name, source, flags] of R[tier]) {
      const re = new RegExp(source, flags + 'g')
      const v = R.validators[name]
      for (const m of text.matchAll(re)) {
        if (v && !VALIDATORS[v](m[0])) continue
        out.push({ tier, name })
        break
      }
    }
  }
  return out
}
function action(text) {
  let a = 'log'
  for (const { tier, name } of hits(text)) {
    a = stricter(a, R.rule_actions[name] || R.default_actions[tier])
  }
  return a
}
// {!} marks secret-shaped fixtures so the repo holds no scannable tokens.
const lines = (f) => fs.readFileSync(path.join(here, f), 'utf8').replaceAll('{!}', '').split('\n').filter((l) => l.trim())

const benign = lines('benign.txt')
const positive = lines('positive.txt')
let failed = false
const stopped = benign.filter((l) => ['block', 'warn'].includes(action(l)))
if (benign.length < 200 || positive.length < 100) { console.error('corpus too small'); failed = true }
if (stopped.length) { console.error('benign lines that block/warn:\n' + stopped.join('\n')); failed = true }
const missed = positive.filter((l) => !hits(l).some((h) => h.tier === 'secret' || h.tier === 'pii'))
const recall = 1 - missed.length / positive.length
if (recall < 0.95) { console.error('missed:\n' + missed.join('\n')); failed = true }
console.log(`benign ${benign.length} lines, ${stopped.length} block/warn; positive ${positive.length} lines, recall ${(recall * 100).toFixed(1)}%`)
// ReDoS guard: no rule may take over 0.5 s on 100 KB of adversarial text.
const N = 100000
const adversarial = {
  'eyJ-': 'eyJ-'.repeat(N / 4), 'eyJ run': 'eyJ' + 'a'.repeat(N), 'a*': 'a'.repeat(N),
  'password spaces': 'password' + ' '.repeat(N), 'password:': 'password:'.repeat(N / 9),
  'sk-': 'sk-'.repeat(N / 3), digits: '4'.repeat(N), 'digits spaced': '4 '.repeat(N / 2),
  'postgres://': 'postgres://' + 'a'.repeat(N), Bearer: 'Bearer '.repeat(N / 7),
  iban: 'GB82 ' + 'ABCD '.repeat(N / 5), '+digits': '+1'.repeat(N / 2), dots: 'a.'.repeat(N / 2),
  'file dots': 'a.b'.repeat(N / 3), 'at signs': 'a@'.repeat(N / 2), 'user@dots': 'a@' + 'b.'.repeat(N / 2),
  'email tail': 'a'.repeat(60) + '@' + 'b'.repeat(N), dashes: '-'.repeat(N), BEGIN: '-----BEGIN '.repeat(N / 11), headers: '-----BEGIN PRIVATE KEY-----'.repeat(N / 27),
}
for (const [label, text] of Object.entries(adversarial)) {
  for (const tier of ['secret', 'pii', 'flag']) {
    for (const [name, source, flags] of R[tier]) {
      const t0 = performance.now()
      ;[...text.matchAll(new RegExp(source, flags + 'g'))]
      const dt = performance.now() - t0
      if (dt > 500) { console.error(`SLOW ${label} ${name} ${(dt / 1000).toFixed(2)}s`); failed = true }
    }
  }
}
process.exit(failed ? 1 : 0)
