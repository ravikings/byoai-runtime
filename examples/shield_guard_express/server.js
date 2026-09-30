const express = require('express')
const { shieldGuard } = require('./shield-guard')

const app = express()
// Read every body as text so the guard sees it whatever it is; it parses JSON itself.
app.use(express.text({ type: '*/*', limit: '8mb' }))
app.use(shieldGuard()) // ahead of your own chat handlers

app.post('/v1/chat/completions', (req, res) => {
  // req.body is already parsed and redacted here; forward it to your model provider.
  res.json({ echo: req.body.messages, shield: req.shield })
})

app.listen(3000, () => console.log('listening on :3000'))
