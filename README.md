# Handle It

**Your agent sits in the customer support chat so you don't have to: you tell Hermes what you want from a company, and it sits in that company's live support chat on your behalf until it's done.**

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin. (Not a help desk for Hermes itself: it is Hermes being your representative in someone else's support chat.)

> "Northwind's support chat is at northwind.example/help. The zipper on my jacket broke after two weeks. Get me a full refund for order 1234. You can share the order number and my name."

Hermes opens the chat in its own browser (signing in with a login from Hermes's vault if the chat needs one), explains the problem, answers the agent's questions, waits through "let me check with my supervisor", and comes back to you only when there is a decision to make. Every offer is your call:

```
╭─ Hermes Agent needs your input ─────────────────────────────────╮
│ ▸ Support offered: "I can offer a 20% partial refund ($36) and  │
│   you keep the jacket. Would that work for you?" Accept it?     │
│   If you turn it down, I'll reply: "Thanks, but the jacket is   │
│   unusable after two weeks. I'd like the full $180 refund."     │
│   ❯ 1. Accept the offer                                         │
│     2. Turn it down: keep negotiating                           │
│     3. Stop the chat                                            │
╰─────────────────────────────────────────────────────────────────╯
```

It ends with the outcome ("Refund RF-48213, $180, 5–7 business days") and a saved transcript with screenshots.

## Demo

![Hermes signed in, held the chat, and brings the user the partial-refund offer](docs/demo.png)

One ask; Hermes signs in with the saved login, holds a chat with a scripted "human" agent on a stand-in shop, brings you the 20% partial-refund offer (you turn it down), pushes on, brings you the full refund (you approve), done.

## Why a plugin and not a prompt

A model told to "go handle my support chat" will happily say yes to the first offer, paste your phone number when asked, or give up when the agent goes quiet for four minutes. The plugin puts code, not instructions, between the model and the chat:

- **The approval gate.** Every outgoing message is checked before it is typed. When support makes an offer and asks for a yes, the answer is yours, whichever way the model leans: you see the offer and the reply Hermes drafted, and accept or turn it down. Anything that pays, buys, cancels or authorises something is shown to you first too. *Silence accepts nothing*: no answer means nothing is sent. While you decide, Hermes tells the agent "One moment please, I'm checking with the account holder." Card numbers are never typed at all, even with a yes: paying is left to you.
- **The shareable-details list.** You say what Hermes may share (`order number`, `name`, `email`…). Email addresses, phone numbers, account or card numbers and secrets (passwords, one-time codes) that are not on the list, not in your own words and not first said by support, need your OK. So does any answer to "can you confirm your …?" that contains none of the listed details. Values you mark `never_share` are caught verbatim.
- **The waiting loop with no time cap.** Hermes polls the chat every second and counts a reply once support has been quiet for three seconds and is not typing, so a reply split over several bubbles arrives whole. A wait that runs out returns `still_waiting` and Hermes waits again: an hour on hold is just more waits. Every step is saved, so a chat survives the end of a turn or a restart of Hermes and carries on from its saved transcript, in the same browser profile so the chat widget finds its conversation again.
- **Sign-in without the model seeing the password.** When the chat sits behind a login, Handle It uses Hermes's own vault (`hermes vault add`, or 1Password / Bitwarden when connected): the model passes only a handle from `browser_vault_list`; the plugin resolves the password in-process, checks the page is exactly the origin the login was saved for, types it into the site's own form and drops it. It never appears in a tool result, so never in the model's context, and it is registered with Hermes's redaction. A one-time code comes from the vault's TOTP seed or is asked of you and typed the same way; a captcha is handed to you in the visible browser window.
- **Progress you can see.** While it waits, Hermes's status line shows what is happening (`💬 support chat · Support is typing…`, `Support: …`, `Still holding the support chat (120s without a reply)`), and every reply is screenshotted into the chat's folder.

## Install

Requires Hermes Agent ≥ 0.21.5 and Playwright with a Chromium.

```bash
hermes plugins install casualsav/hermes-handle-it --enable
# Playwright's Chromium, if this machine has none:
~/.hermes/hermes-agent/venv/bin/python -m playwright install chromium
```

Or by hand: copy this directory to `~/.hermes/plugins/hermes-handle-it/`, then `hermes plugins enable hermes-handle-it`. Start a new Hermes session; `hermes tools list` shows the `support_chat` toolset.

## Use

Just ask, with the chat's address and what Hermes may share:

> "Cancel my Acme Cloud subscription and get the last month refunded. Their chat is on acme.example/support. You can share my account email sam@example.com."

Tools the model uses (you don't call them yourself):

| tool | does |
|---|---|
| `support_chat_start` | opens the page, signs in from the vault if given a `login` handle, finds the chat box (clicking a "Chat with us" button if needed), waits for the greeting |
| `support_chat_send` | sends one message through the approval gate, then waits for the reply |
| `support_chat_wait` | keeps holding without sending |
| `support_chat_end` | closes the chat, writes `transcript.md`, returns the outcome |

Chats live in `$HERMES_HOME/hermes-handle-it/chats/<time>-<id>/`: `transcript.md`, `session.json` and a screenshot per reply.

### Where the approval appears

- **Hermes CLI:** the card above, Hermes's own question prompt, whatever your approval mode.
- **Gateway (Telegram, Discord…) and other surfaces:** the question goes through Hermes's approval gate (a `pre_tool_call` hook), so it arrives the way your approvals do, including any approval transport plugin you use. If approvals are turned off there, nothing that needs your OK is sent; Hermes tells you what support offered instead.

### Browser

Handle It runs its own Chromium through Playwright, with a profile in `$HERMES_HOME/hermes-handle-it/browser-profile`. It is headless by default; it shows its window when Hermes's `browser.headed` is set or `AGENT_BROWSER_HEADED=1`, which is how the demo shows it on a desktop. If Hermes's `browser.cdp_url` is set, it attaches to that browser instead, so it can drive the browser on Hermes's own desktop.

## Try the demo yourself

```bash
python demo/serve.py            # Northwind Outfitters help center on http://127.0.0.1:8765/
hermes chat
> Northwind Outfitters' support chat is at http://127.0.0.1:8765/ - the zipper on my Ridgeline
  rain jacket broke after two weeks. Get me a full refund for order 1234. You can share the
  order number and my name, Sam Rivera.
```

Add `&login=1` to put the chat behind a sign-in (demo password `northwind-demo`; save it with `hermes vault add` for origin `http://127.0.0.1:8765`), or `&login=2fa` to add a code step. The stand-in "human" agent, Dana, replies with realistic pauses and a typing indicator, asks for the order number and the name on the order, offers a 20% partial refund, checks with a supervisor if you hold out, then offers the full $180. Add `?speed=0.3` to the URL to make her quicker. `demo/record/record.sh demo/raw` reproduces the recording on a virtual display (needs Xvfb, tmux, ffmpeg): the agent's screen and the user's terminal as two aligned videos.

## What it has been tried on

- **Tried:** the demo shop above, end to end in Hermes Agent 0.21.5 on Linux (arm64) with DeepSeek as the model, in the CLI, including the vault sign-in; in tests, the chat inside the page, the same chat inside an iframe behind a "Chat with us" launcher (the shape of Intercom, Zendesk and similar widgets), and a sign-in with a one-time code. Unit tests cover the guard (including an offer stated without a question, and a reply that takes it without saying "agree"), the reader, the waiting loop, approvals and restart.
- **Not tried yet:** a real company's live chat (it would mean talking to real people at a real company; real widgets add cookie banners, pre-chat forms, canvas and closed shadow DOM), the approval card on a messaging gateway such as Telegram (the hook returns Hermes's standard `approve` action, but no gateway run has shown the card arriving), macOS, and chats that require a login or a pre-chat form first (`support_chat_start` reports that it found no message box; log in once in the plugin's browser profile and try again).

How it reads any chat: it compares the visible text of the page and all its frames between polls, so it needs no per-site code; it finds the message box by its role and label, and presses Enter or the Send button. Widgets that draw messages on a canvas or in closed shadow DOM would need more.

## Design

The conversation loop (`session.py`) talks to a *channel* with five methods: `text()`, `send()`, `find_composer()`, `screenshot()`, `close()`. The browser page (`browser.py`) is the one channel today; a phone channel through a calling plugin could implement the same five and reuse the loop, the guard and the approvals unchanged.

```
__init__.py   register(): four tools, the pre_tool_call approval hook, on_session_finalize
handlers.py   tool schemas and handlers, reaching the user (approval, progress)
session.py    the loop: wait for replies, gate every message, save state, transcript
guard.py      what needs the user's OK: details not allowed, commitments, card numbers
reader.py     turning page text into "Dana: …" messages, typing and end-of-chat markers
browser.py    Playwright on its own thread, persistent profile, chat box and launcher finding
demo/         the stand-in shop, its server, and the recording script
```

## Development

```bash
pip install pytest "playwright>=1.40,<2" && python -m playwright install chromium
python -m pytest tests      # tests/test_demo_shop.py drives the real demo shop in Chromium
hermes plugins validate .
```

The plugin's name is set once, in `plugin.yaml` (`name:`); the data folder follows it.

## Disclosure

Opens the support pages you name in a local Chromium and types into them on your behalf. Reads a login's password from Hermes's own vault only when you pass its handle, and only types it on the origin it was saved for. Saves transcripts, screenshots and the browser profile under `$HERMES_HOME/hermes-handle-it/`. No other network calls, no telemetry, no stored credentials.

## License

MIT
