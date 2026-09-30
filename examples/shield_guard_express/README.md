# Shield guard for Express

`shield-guard.js` runs Shield's rules on JSON chat bodies (`/v1/chat/completions`,
`/v1/messages`, `/v1/responses`): secrets get a `403` with type
`coriqo_shield_blocked`, personal details become `[EMAIL_1]`-style placeholders.

It mirrors the Python rules by reading the generated `shield-rules.js`
(`npm run sync-rules` copies it from `src/byoai/browser_extension/`). Its
`rules_version` names the rule set; re-sync after upgrading byoai-runtime.

    npm install && npm test && npm start
