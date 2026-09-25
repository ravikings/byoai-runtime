# Coriqo Shield — Agent Capture: privacy posture

This extension records *that* you sent a message to an AI chat, never *what
you said*. This file is the data-protection record for reviewers ( GDPR
Art. 30 register entry equivalents noted inline) and for anyone auditing the
code — it is short on purpose, and every claim links to the line that makes
it true.

## What is collected

Per message you send on claude.ai or chatgpt.com:

| Field | Example | Why |
|---|---|---|
| `kind` | `browser.chat.request` | what happened |
| `app` | `chatgpt` | which surface |
| `chars` | `42` | message length only |
| `wire` | `/backend-api/conversation` | truncated endpoint path |
| `sent_at` | ISO timestamp | when |

Nothing else. There is no read of page content, message text, DOM, cookies,
form fields, or clipboard. The message text is counted (`chars = …length`)
and immediately discarded — it exists as a variable for fractions of a
second, is never stored, never logged, never transmitted.

## What is NOT collected

- Message content or previews (see `clean_browser_row` on the server side:
  any unexpected field, text included, is dropped before the ledger).
- Browsing history, tabs, or keystrokes outside the two chat surfaces.
- Identifiers of any kind. No account, device, or user ID is attached;
  identification happens at the Shield server by localhost origin only.
- No analytics, no telemetry, no third-party requests. The only network
  destination is the local Shield server, and the manifest's
  `host_permissions` is limited to `127.0.0.1`/`localhost` so it cannot be
  widened without a new install.

## Where data goes

Rows ship to `POST http://127.0.0.1:8300/api/browser` (configurable but
*enforced* localhost — see `isLocalEndpoint` in `background.js`). One
destination, on the same machine, behind the guard the server already runs
against web pages. If Shield is not running, rows wait in memory (capped at
50) and are dropped after that — a blind spot, never a stored record
(`MAX_QUEUED` in `background.js`).

## Retention

- In the extension: memory as the working set, mirrored to
  `chrome.storage.session` (cleared when the browser closes) so a service-
  worker restart doesn't drop rows mid-flight; ≤ 50 rows, and when the
  local server stays off, older rows are dropped rather than kept. No
  capture data touches `chrome.storage.local` — that permission stores
  only the endpoint URL string.
- On the Shield server: the ledger's existing privacy-first rules apply
  (retention days, scrub pass, sealed-but-text-free payloads — see
  `README.md` § Privacy-first by default).

## Legal basis and transparency

The Shield server's user-facing notice strip (web UI, Trust tab) states what
is checked and kept, and is not dismissible by design. Because the shield is
a local tool operated by the same person whose machine it runs on (household
admin setting it up for the Mac's users), the transparency duty is met by
that strip plus this file; there's no controller-processor split and no
third-country transfer — data never leaves the machine.

## User rights in practice

- Access & portability: `/api/receipt/<seal>` exports a verifiable receipt.
- Erasure: `POST /api/privacy/scrub` (Settings → Privacy → Remove now).
  Seals stay; servers can't unsee what's sealed — that is the tamper
  evidence working, and it holds only what was documented above.
- Objection / opt-out: close the toggles on the affected apps in Shield's
  Settings, or remove the extension. Data-extraction stops at the source.

## Integrity, not secrecy

Everything collected is sealed into an RFC 6962 Merkle chain signed with a
key held on the same Mac (`byoai.recorder.keys`). There is no "silent
monitoring" claim to worry about: the sealed record is *open* to the person
it describes, and its integrity can be proven to anyone via receipts —
while its content remains metadata-only.
