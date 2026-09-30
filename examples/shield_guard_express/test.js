// node test.js (after `npm run sync-rules`)
const assert = require('assert')
const { check } = require('./shield-guard')
const body = (t) => ({ messages: [{ role: 'user', content: t }] })
assert.deepStrictEqual(check(body('key sk-ant-' + 'a1B2c3D4e5F6g7H8i9J0k1L2')).blocked.includes('anthropic_key'), true)
const r = check(body('mail bob@example.com'))
assert.strictEqual(r.body.messages[0].content, 'mail [EMAIL_1]')
assert.strictEqual(check(body('hello')).blocked.length, 0)
const AWS = 'AKIA' + 'Q3ZT7XWPL2MN4RJK'
assert.ok(check({ messages: [], metadata: { user_id: AWS } }).blocked.includes('aws_access_key'))
assert.ok(check({ messages: [{ content: [{ type: 'tool_use', id: AWS }] }] }).blocked.length > 0)
const b64 = Buffer.from('key ' + AWS).toString('base64')
assert.ok(check({ messages: [{ content: [{ type: 'document', source: { type: 'base64', data: b64 } }] }] }).blocked.length > 0)
assert.strictEqual(require('./shield-guard').norm('/V1/Chat/Completions/'), '/v1/chat/completions')
// middleware: string bodies, non-JSON text, path normalisation of configured paths
const { shieldGuard } = require('./shield-guard')
const run = (mw, req) => { const out = {}; mw(req, { status: (c) => ({ json: (j) => { out.status = c; out.json = j } }) }, () => { out.next = true }); return out }
const mw = shieldGuard({ paths: ['/V1/Chat/Completions/'] })
assert.strictEqual(run(mw, { method: 'POST', path: '/v1/chat/completions', body: JSON.stringify({ m: AWS }) }).status, 403)
assert.strictEqual(run(mw, { method: 'PUT', path: '/v1/chat/completions/', body: 'raw ' + AWS }).status, 403)
assert.strictEqual(run(mw, { method: 'POST', path: '/v1/chat/completions', body: 'plain text' }).next, true)
const dup = '{"a":"' + AWS + '","a":"hi"}'
assert.strictEqual(run(mw, { method: 'POST', path: '/v1/chat/completions', body: dup }).status, 403)
const red = { method: 'POST', path: '/v1/chat/completions', body: JSON.stringify({ m: 'a@b.co' }) }
run(mw, red); assert.strictEqual(red.body.m, '[EMAIL_1]')
console.log('ok')
