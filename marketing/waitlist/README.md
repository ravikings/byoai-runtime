# Coriqo waitlist page

A single static page (`index.html`) with two variants for the wl1 demand test.

- `experiment-plan.md`: what we're testing, thresholds, timeline, scorecard
- `ads.md`: ad copy, targeting, budget split and tracking links

## Variants

The variant comes from the URL:

- `?v=a` shows **AI antivirus** (consumer data leaks and hijacked pages)
- `?v=b` shows **Agent control** (action log, approvals, tamper-evident)
- `#a` / `#b` work too
- A visitor with no variant gets a random one, kept for return visits

Every signup records `variant` plus `utm_source`, `utm_medium`,
`utm_campaign`, `utm_content` and the referrer.

## Deploy (Netlify, about 10 minutes)

1. Sign in at netlify.com. Choose **Add new site → Deploy manually** and drag
   this `waitlist` folder in.
2. In **Site configuration → Forms**, enable form detection, then redeploy
   (drag the folder in again). Two forms should appear: `waitlist` and
   `waitlist-details`.
3. In **Forms → waitlist → Settings**, add an email notification so you see
   signups arrive.
4. **Domain:** add `coriqo.io` under **Domain management**, then follow
   Netlify's DNS instructions at your registrar.
5. Open the live URL with `?v=a`, sign up with your own email, and check the
   submission shows `variant=a`. Repeat with `?v=b`.

Netlify's free tier includes 100 form submissions a month. If signups pass
that, upgrade to Forms Level 1 (about $19/month), or switch to Formspree by
setting `CONFIG.endpoint` in the script.

**Preview mode.** On any host not listed in `CONFIG.liveHosts`
(`coriqo.io` and `*.netlify.app`), the page shows a variant switcher and does
not send signups. If you deploy elsewhere, add that host to the list or
signups will be silently dropped.

## Analytics (Plausible)

1. Add the site at plausible.io.
2. Uncomment the two script lines in the `<head>` of `index.html`.
3. In Plausible, add goals for the custom events **Waitlist Signup**,
   **Founding Reserve** and **Survey Complete**, and enable the custom
   properties `variant`, `source` and `ad`.

Plausible doesn't use cookies, so no consent banner is needed. If you add
the Reddit or X pixel for conversion tracking, you'll need a consent banner
for EU and UK visitors.

## Before launch checklist

- [ ] `CONFIG.contactEmail` is an inbox someone reads (currently
      `hello@coriqo.io`)
- [ ] Both variants tested end to end on the live domain
- [ ] Plausible goals firing (check the Realtime view while you sign up)
- [ ] Ad links from `ads.md` use the live domain
- [ ] Decision thresholds in `experiment-plan.md` agreed before any ad spend
