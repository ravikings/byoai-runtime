# Coriqo waitlist experiment (wl1)

**Question:** Will strangers ask to put Coriqo between themselves and their AI,
and which angle pulls harder: consumer "AI antivirus" (A) or agent control (B)?

**Spend:** $1,000 paid, plus a free developer track run alongside it.
**Duration:** 14 days from page build to decision.

## What $1K can and can't tell us

At $1.50–3 per click we expect **350–650 visitors, about 175–325 per
variant**. At that size a signup rate is only accurate to roughly ±3–4
points. We can tell 3% from 12%. We can't tell 7% from 9%. Treat small gaps
as a tie.

This test measures **stated demand**, meaning whether people ask for it. It
says nothing about retention or willingness to pay monthly. Those need a
product.

## Metrics

Taken from the Netlify form exports and Plausible, per variant and per ad:

| Metric | Definition | Where |
|---|---|---|
| CTR | clicks ÷ impressions | Ad platform |
| CPC | spend ÷ clicks | Ad platform |
| Visitors | unique visitors with that `utm_content` | Plausible |
| **Signup rate** | `waitlist` submissions ÷ visitors | Netlify + Plausible |
| **Cost per signup** | spend ÷ signups | computed |
| Survey rate | `waitlist-details` submissions ÷ signups | Netlify |
| **Reserve rate** | `reserved = yes` ÷ signups | Netlify |
| Worry mix | share of each `worry` answer | Netlify |
| Tool mix | share using 2+ AI tools, share using agents | Netlify |

The **reserve rate** is the strongest signal. An email costs nothing to give.
Clicking "reserve founding price" is a stated intent to pay.

## Decision thresholds (set before launch; don't move them afterwards)

Judge each variant on its own:

| Result | Signup rate | Reserve rate (of signups) | Decision |
|---|---|---|---|
| **Dead** | under 3% | under 10% | Drop this angle as written |
| **Interest** | 3–10% | 10–25% | Rewrite the page and run a $2–3K round on this angle |
| **Pull** | over 10% | over 25% | Build a thin version and invite the waitlist in batches |

Then compare A with B:

- If one is **Pull** and the other isn't, that's the direction.
- If both are similar, look at **reserve rate and cost per signup** before
  signup rate. Fewer people with more intent to pay beats more casual ones.
- If both are **Dead**, don't conclude "no market". Look at CTR first. Low
  CTR means the ad failed. Good CTR followed by low signup means the page
  failed.

Also look at: the worry answers from A's signups. If most of A's signups pick
"An AI doing something I didn't want" or "Not knowing what my AI did", they
are asking for B's product.

## Free developer track (same two weeks)

This tests the segment the code already serves, at no ad cost.

1. **Demo video, 60–90 seconds.** An agent runs through `byoai-runtime`'s
   proxy; the recorder logs each call; the console shows the record. Show a
   verify step failing when an entry is edited.
2. **Show HN**, posted Tuesday–Thursday around 8–9am US Eastern. Draft below.
3. Posts in r/LocalLLaMA and r/AI_Agents (as a person sharing work, not an
   ad), and in any AI-engineering Discords or Slacks you're already in.
4. Link to `https://coriqo.io/?v=b&utm_source=<hn|reddit-organic|discord>&utm_medium=organic&utm_campaign=wl1`
   so these signups are counted separately from paid.

**Check before posting.** Every claim in the post has to work for a stranger
running it from a clean machine. As of this branch the console can read
evidence, but a recorder can't ship into it yet (there is no
`/v1/ingest/batch`). Either close that gap first or keep the demo to what
works locally today.

### Show HN draft

> **Show HN: Coriqo – a tamper-evident record of what your AI agents did**
>
> We run agents against email, files and APIs, and kept hitting the same
> problem: when something went wrong, there was no trustworthy record of what
> the agent actually did or who asked it to.
>
> Coriqo sits between your agent and the model API as a proxy. It records
> every call and tool action into a hash-chained log on your machine. Each
> entry commits to the one before, so an edited or deleted entry fails
> verification. It works with Anthropic, OpenAI, Gemini and local models, and
> needs no changes to your agent code beyond the base URL.
>
> `pip install byoai-runtime` then `byoai-cache console` gets you the proxy
> and a local UI.
>
> Next up is holding risky actions (external sends, payments, deletes) for
> approval. We'd especially like to hear from people running agents with
> real permissions: what would you want held, and what would just be noise?
>
> [repo link] · [waitlist link]

## Timeline

| Days | Work |
|---|---|
| 1–3 | Deploy page, connect domain, set up Plausible goals, submit ads for review, record the demo |
| 4 | Ads go live. Check at hour 6 that signups arrive in Netlify with the right variant and UTMs |
| 4–13 | Ads run. Daily 10-minute check: spend, CTR, pause ads under 0.3% CTR after $60 |
| 5–7 | Show HN and organic posts |
| 14 | Pull exports, fill the scorecard, decide using the thresholds above |

## Scorecard (fill on day 14)

| | A paid | B paid | Organic (dev) |
|---|---|---|---|
| Spend | | | $0 |
| Clicks / visitors | | | |
| Signups | | | |
| Signup rate | | | |
| Cost per signup | | | n/a |
| Reserves | | | |
| Reserve rate | | | |
| Top worry | | | |
| Verdict (Dead / Interest / Pull) | | | |

## Obligations this test creates

- **Founding price.** Anyone who clicked "reserve" was promised $5/month
  locked in, with no charge until they choose. Honour it.
- **Privacy.** The page says what's stored and how to delete it. Answer
  deletion requests to the contact address, and don't add the list to other
  marketing tools without telling people.
- **"Runs on your device" / "tamper-evident."** These appear in the page
  copy as product commitments. If the product changes, change the page.
