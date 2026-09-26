# Coriqo Shield: Agent Capture — privacy notice

This extension records *that* you sent a message to an AI chat, never *what you
said*. It does nothing until you agree on the welcome page it opens when you
install it, and you can turn it off again at any time from its popup.

## What it does

When you send a message on claude.ai, chatgpt.com (and chat.openai.com),
gemini.google.com or copilot.microsoft.com, the extension notes the fact and
hands it to **Shield, a separate program running on your own computer**
(`byoai-shield`). No other site is watched. Without Shield running, nothing is
recorded.

## What is collected

Per message you send:

| Field | Example | Why |
|---|---|---|
| `kind` | `browser.chat.request` | what happened |
| `app` | `chatgpt` | which app |
| `chars` | `42` | message length only |
| `wire` | `/backend-api/conversation` | truncated endpoint path |
| `sent_at` | ISO timestamp | when |

Nothing else. The extension does not read page content, the DOM, cookies, form
fields, browsing history or the clipboard. The message text is counted in the
page (`chars = text.length`) and immediately discarded: it is never stored,
logged or transmitted. The server side drops any unexpected field, text
included, before it reaches the record (`clean_browser_row`).

No account, device or user identifier is attached to rows. There is no
analytics, no telemetry, no advertising and no third-party request. The data is
never sold, and is not used or transferred for anything other than the single
purpose above (noting that a message was sent, on this computer).

## Where it goes

Only to `http://127.0.0.1:17831/api/browser` (the port is configurable, but the
extension refuses any address that is not this computer: see `isLocalEndpoint`
in `background.js`). The manifest's `host_permissions` are limited to
`127.0.0.1` and `localhost`, so this cannot be widened without a new release.
The extension only sends to a Shield that proves it holds the key it first
paired with; a different program on the same port receives nothing.

What Shield does with its own record on your computer is described in its
README. Shield can, if you choose to enrol it in Coriqo, send a signed
checkpoint (a hash and a count, no content) to a Coriqo tenant; that is a
setting of Shield, off by default, not of this extension.

## What the extension keeps in your browser

| Where | What | Lifetime |
|---|---|---|
| `chrome.storage.session` | rows waiting for Shield, at most 50 | cleared when the browser closes; older rows are dropped, never kept |
| `chrome.storage.local` `consent` | that you agreed, and when | until you turn capture off |
| `chrome.storage.local` `endpoint` | Shield's local address, if you changed it | until removed |
| `chrome.storage.local` `pinned_shield` | Shield's public key, saved when first paired | until you re-pair |
| `chrome.storage.local` `witness` / `rollback` | the highest count of sealed entries Shield reported (one number), and a flag if it later dropped | until you re-pair or accept |
| `chrome.storage.local` `last_capture` / `today` | one "last message noted" time per app, and today's message count | overwritten on each send; removed when you turn capture off |

None of this contains message text, and none of it leaves your computer.

## Permissions, and why

- `storage`: the items above.
- `alarms`: retry sending to Shield if it was not running yet.
- `activeTab`: lets the popup show whether the tab you are looking at is
  covered, only when you open it.
- Host access to `127.0.0.1` / `localhost`: to reach Shield.
- Content scripts on the five chat sites listed above: to notice a send. One
  runs in the page so it can see the send request (it must wrap `fetch`, since
  the sites' security policies block injecting anything else); the other
  relays the length-only fact to the extension. Neither loads remote code. All
  code ships in the package.

## Your choices

- **Turn it off:** popup → Details → *Turn capture off*. This stops recording
  and removes anything still waiting to be sent, plus the per-app times and the
  count above.
- **Remove it:** uninstalling the extension stops capture at the source.
- **Erase what Shield already kept:** Shield's Settings → Privacy → *Remove
  now* (`POST /api/privacy/scrub`). Sealed entries stay, because removing
  them would break the seal; they hold only the fields listed above.
- **Access:** `/api/receipt/<seal>` on Shield exports a verifiable receipt.

## The record Shield keeps

Shield seals each entry into a Merkle chain signed with a key that stays on your
computer. That lets you prove the record was not edited afterwards. It holds
only the fields listed above. The extension asks before it starts, and its
popup always shows whether it is on.

## Contact

Questions or concerns: https://github.com/ravikings/byoai-runtime/issues
