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
| `support_call_wait` | keeps following it: new lines from the call, your approvals, the end of the call |
| `support_call_end` | hangs up if still on the line, writes `transcript.md` with the outcome, the cost and the check |

Calls live in `$HERMES_HOME/<plugin name>/calls/<time>-<id>/`.

## How approval works on a live call

bland.ai runs the conversation itself, and it has no way to send it a message mid-call. What it
does have is a custom tool, an HTTPS request it makes during the call. Each call gets one,
`CheckWithAccountHolder`. The voice agent is told to use it before it agrees to anything:

1. It says "One moment please, I'm checking with the account holder." and sends what support
   said and what it would answer to the plugin's bridge.
2. The bridge holds the request. Hermes shows you the offer and the drafted answer, with three
   choices: *Yes, go ahead*, *No: keep working toward the goal*, *End the call*. You can also type
   an instruction instead.
3. bland.ai waits on a tool for at most 60 seconds. If you haven't answered after 45, the bridge
   answers `pending`. The agent asks support to keep holding, then asks the bridge again.
4. If nobody can be asked or nobody answers, nothing is agreed. The agent says you will follow up
   and ends the call.

The bridge listens on `127.0.0.1` at the plugin setting `call_bridge_port` (default 8788).
bland.ai has to reach it over public HTTPS, so set the plugin setting `call_bridge_url`
(`plugins.entries.hermes-handle-it.settings.call_bridge_url`) to a tunnel or reverse proxy that forwards
to that port (for example `cloudflared tunnel --url http://127.0.0.1:8788`). Every request has to
carry the call's own random bearer token, or the bridge answers 404.

**Without a bridge**, the agent is told it can agree to nothing. It gets the offer repeated and
noted on the case, then ends the call, and the offer comes back to you afterwards.

**What code enforces and what it doesn't.** The voice agent speaks without going through this
plugin. The guarantees are:

- It is never told a detail you didn't allow, and `never_share` values never leave your machine.
- Its yes depends on your answer through the bridge.
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
  maximum minutes and whether it is recorded.
- A public HTTPS address for the bridge (above). Without one, calls run but cannot accept offers.
- Nothing else. Outbound calls go out from a bland.ai-owned caller ID unless you pass your own
  number, and no phone number has to be bought.

## What it costs

These are bland.ai's prices, read on 2026-11-11 at
[docs.bland.ai/platform/billing](https://docs.bland.ai/platform/billing.md):

- On the free Start plan, each connected minute costs $0.14.
- Each call attempt costs $0.015 at minimum, and a failed call is charged too.
- Paid plans lower the per-minute price: $299 a month gets $0.12, and $499 a month gets $0.11.
- The Start plan allows 100 calls a day.

A 10-minute support call costs about $1.40. An hour on hold costs $8.40, so `max_minutes`
(default 30, at most $4.20) caps every call.

## What it has been tried on

On 2026-11-11, one real call went to Best Buy's customer-care line. The question was a general
one: what is the return window for online orders. No account details were shared. As soon as it
was answered, the agent said it was an AI assistant and that the call was being recorded.
Best Buy's own AI assistant answered. The agent got past its menu and came back with the
policy: 15 days for most products, 60 for members. It turned down the offer of a text. The call
took 1.6 minutes and cost $0.22. Real bland.ai prices and summarises a call a few seconds after
marking it completed, so `support_call_wait` waits for the price before it returns.

`record: true` keeps bland.ai's recording, and the agent then says the call is being recorded.
