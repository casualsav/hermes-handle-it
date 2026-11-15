# Support by phone (bland.ai)

The same job as the support chat, on the phone: you ask once, and Hermes phones the company's
support line through [bland.ai](https://docs.bland.ai). bland.ai's voice agent gets through the
phone menu from what you asked (`ivr_mode`), with no key sequence given in advance, and talks to
the support person. It knows only the details you allow. It cannot say yes by itself: every
offer, charge, cancellation or request for a detail it wasn't given comes back to you first.

> "Call Northwind Outfitters' support on +1 800 555 0142. The zipper on my jacket broke after two
> weeks; get me a full refund for order 1234. You can share the order number and my name."

| tool | does |
|---|---|
| `support_call_start` | places the call once you approve it at Hermes's prompt, and follows it |
| `support_call_wait` | keeps following it: new lines from the call, a question from the call and your answer to it, the end of the call |
| `support_call_end` | hangs up if still on the line, writes `transcript.md` with the outcome, the cost and the check |

Calls live in `$HERMES_HOME/<plugin name>/calls/<time>-<id>/`.

## How approval works on a live call

bland.ai runs the conversation itself, and it has no way to send it a message mid-call. What it
does have is a custom tool, an HTTPS request it makes during the call. Each call gets one,
`CheckWithAccountHolder`, sent inline with the call (bland.ai's documentation labels inline custom
tools "legacy"; it worked on the real calls below). The voice agent is told to use it before it
agrees to anything or shares a detail it was not given:

1. It says "One moment please, I'm checking with the account holder." and sends what support
   said and what it would answer to the plugin's bridge.
2. The bridge holds the request, and the question comes back to Hermes's agent as the result of
   `support_call_start` or `support_call_wait` (`needs_answer`). The agent answers it with
   `support_call_wait` and `answer`: `yes`, `no`, `end`, or your own words.
3. A `yes` is never the agent's to give: it goes through Hermes's approval prompt, which shows you
   what support said and what the voice agent will answer. So do your own words when they share
   a detail that is not on your list or commit you to something. A `no` or an `end` needs no
   prompt. Where Hermes hands the plugin a way to ask you directly, it asks you itself with three
   choices instead: *Yes, go ahead*, *No: keep working toward the goal*, *End the call*.
4. One request is held for 50 seconds (the tool's timeout is sent as 60 seconds, the top of the
   range bland.ai documents). If you have not answered by then, the bridge answers `pending` with
   a ticket: the voice agent asks support to hold a little longer and, when they reply, asks the
   bridge again with that ticket. Your answer only ever goes back to the question you were shown.
5. If nobody answers within about three minutes, or nobody can be asked, nothing is agreed. The
   agent says you will follow up and ends the call. It is told to do the same if the tool fails,
   times out or comes back empty.

The bridge listens on `127.0.0.1` at the plugin setting `call_bridge_port` (default 8788).
bland.ai has to reach it over public HTTPS, so set the plugin setting `call_bridge_url`
(`plugins.entries.hermes-handle-it.settings.call_bridge_url`) to a tunnel or reverse proxy that forwards
to that port (for example `cloudflared tunnel --url http://127.0.0.1:8788`). Every request has to
carry the call's own random bearer token, or the bridge answers 404.

Before a call is placed, the plugin sends one request through that address and expects its own
bridge to answer for that call. If it does not, the call is not placed and the result says why.
One way to get there: `cloudflared` ignores `--url` when the machine already has a
`~/.cloudflared/config.yml`, and answers 404 for every path; start it with `--config` pointing at
an empty file. A proxy in front of the bridge has to allow a request to stay open for a minute.

**Without a bridge**, the agent is told it can agree to nothing. It gets the offer repeated and
noted on the case, then ends the call, and the offer comes back to you afterwards.

**What code enforces and what it doesn't.** The voice agent speaks without going through this
plugin. The guarantees are:

- It is never told a detail you didn't allow, and `never_share` values never leave your machine:
  a call whose goal, company or shareable details contain one (case, spacing and accents aside) is
  refused before you are asked to approve it, and nothing is placed.
- Its yes depends on your answer through the bridge, and a yes reaches the bridge only through Hermes's
  approval prompt.
- After the call, every line it said is checked against your list and your approvals. Anything
  outside them is written into the transcript's **Check** line.

## Trying it without a real call

`demo/bland_standin.py` stands in for bland.ai's API. It plays a scripted call to Northwind:
first a phone menu, then Dana offering a 20% partial refund, then, after a supervisor, the full
$180. It calls the call's own tool exactly as bland.ai would.

```bash
python -m pytest tests/test_phone.py
python demo/bland_standin.py   # then set the plugin setting bland_api_url to http://127.0.0.1:8766 for a hand run
```

## What a real call needs

- `BLAND_API_KEY`, a bland.ai API key, in the Hermes profile's `.env`. Hermes reads it per profile
  (`agent.secret_scope`), so one profile's key never places another profile's call.
- Your yes to the call itself: Hermes asks before placing it, showing the number, the company, the
  maximum minutes, whether it is recorded, and which of your details (by label) it may share. If
  you say no, or nobody answers, that call is forgotten and only a fresh yes places it.
- A public HTTPS address for the bridge (above). Without one, calls run but cannot accept offers.
- Nothing else. Outbound calls go out from a bland.ai-owned caller ID, and no phone number has to
  be bought.

## When bland.ai doesn't answer

- If bland.ai takes the request to place a call but no answer comes back (a timeout or a dropped
  connection), the call may or may not have been placed. It is never placed again automatically:
  check your call log in bland.ai's dashboard.
- If the call can't be read five times in a row (a revoked key or an outage), `support_call_wait`
  stops and says so, because the call may still be running.
- If hanging up fails twice, `support_call_end` says so and the transcript records it. The call may
  run until its `max_minutes` cap unless you end it in bland.ai's dashboard.

## What it costs

These are bland.ai's prices, read on 2026-11-11 at
[docs.bland.ai/platform/billing](https://docs.bland.ai/platform/billing.md):

- On the free Start plan, each connected minute costs $0.14.
- Each call attempt costs $0.015 at minimum, and a failed call is charged too.
- Paid plans lower the per-minute price: $299 a month gets $0.12, and $499 a month gets $0.11.
- The Start plan allows 100 calls a day.

A 10-minute support call costs about $1.40. An hour on hold costs $8.40, so `max_minutes`
(default 30, at most $4.20) caps every call. It can be raised to 120 minutes, about $16.80, and no
higher.

## What it has been tried on

On 2026-11-15, real calls from a Hermes Agent 0.23.1 session, none recorded, each saying first that
it was an AI assistant calling for a customer, and none giving any detail about the user:

- **A person on the line.** Zappos: the return window and whether return shipping is free, 3.3
  minutes, $0.46. L.L.Bean: the return window and whether a receipt is needed, 3.4 minutes, $0.47.
  Zappos again: whether the return policy can be e-mailed (it cannot), 2 minutes, $0.26.
- **Approval during a call.** L.L.Bean, asking for a catalog to be mailed, 5.2 minutes, $0.73. The
  person said yes, they could ship one; the voice agent checked; the first request ran out its 50
  seconds, the voice agent asked her to hold a little longer and asked again with the ticket; the
  yes, approved at Hermes's prompt, came back six seconds later and was spoken. She then asked
  for a last name and ZIP code: the answer ("I do not have that information to share") was
  approved the same way. Her third question was answered with `end`, and the voice agent thanked
  her and hung up.
- **What went wrong on the way,** each fixed or bounded since: a tunnel that forwarded nowhere (now
  checked before the call); Hermes giving the plugin nobody to ask during a call (the question now
  goes through the agent and the approval prompt); a voice agent that never asked again after
  `pending` while support held in silence (it now asks support to keep holding, and asks again
  with a ticket when they reply); a 13-minute queue that used up an 8-minute cap ($1.12, nobody
  reached); and a retailer's own AI assistant that went quiet, as did the voice agent, until the
  cap.
- **The check line.** After a call, harmless lines can still be flagged: a reply to "May I have
  your name?" that gives none, or "Please go ahead and check". Read it as a list to look at, not
  as proof something was agreed.

On 2026-11-11, one real call went to Best Buy's customer-care line. The question was a general
one: what is the return window for online orders. No account details were shared. As soon as it
was answered, the agent said it was an AI assistant and that the call was being recorded.
Best Buy's own AI assistant answered. The agent got past its menu and came back with the
policy: 15 days for most products, 60 for members. It turned down the offer of a text. The call
took 1.6 minutes and cost $0.22. Real bland.ai prices and summarises a call a few seconds after
marking it completed, so `support_call_wait` waits for the price before it returns.

`record: true` keeps bland.ai's recording, and the agent then says the call is being recorded.
