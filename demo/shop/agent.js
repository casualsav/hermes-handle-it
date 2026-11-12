// A scripted "human" support agent for the demo shop. It answers with
// realistic pauses and a typing indicator, asks for an order number and the
// name on the order, offers a partial refund first and a full one if the
// customer holds out. ?speed=0.1 makes every pause ten times shorter.
(function () {
  const params = new URLSearchParams(location.search);
  const speed = Number(params.get("speed")) || 1;
  // ?login=1 puts the chat behind a sign-in (demo password: northwind-demo); ?login=2fa adds a code step.
  const login = params.get("login");
  const AGENT = "Dana";
  const log = document.getElementById("log");
  const input = document.getElementById("input");
  const send = document.getElementById("send");
  const typing = document.getElementById("typing");
  const presence = document.getElementById("presence");
  const agentName = document.getElementById("agent-name");
  let state = "connecting";
  let order = null;
  let busy = false;
  const queue = [];

  function add(kind, who, text) {
    const div = document.createElement("div");
    div.className = "msg " + kind;
    if (who) {
      const w = document.createElement("span");
      w.className = "who";
      w.textContent = who;
      div.appendChild(w);
    }
    div.appendChild(document.createTextNode(text));
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  const wait = (s) => new Promise((r) => setTimeout(r, s * 1000 * speed));

  async function say(text, think) {
    await wait(think || 2);
    typing.textContent = AGENT + " is typing…";
    await wait(Math.min(6, 1.5 + text.length / 40));
    typing.textContent = "";
    add("agent", AGENT, text);
  }

  const yes = /\b(yes|yeah|yep|sure|ok(ay)?|accept|agree|go ahead|please do|sounds good|that works|proceed|please process)\b/i;
  const no = /\b(no|not|decline|rather|prefer|full refund|instead)\b/i;

  const holding = /\b(one moment|checking with|hold on|give me a (moment|minute))\b/i;

  async function respond(text) {
    if ((state === "partial" || state === "full") && holding.test(text) && text.split(/[.!?]/).filter((x) => x.trim()).length <= 1) {
      return say("Sure, take your time.", 1);
    }
    switch (state) {
      case "issue": {
        const m = text.match(/\b\d{3,}\b/);
        if (m) { order = m[0]; return verify(); }
        state = "order";
        return say("I'm sorry to hear that. Could you share the order number so I can look it up?");
      }
      case "order": {
        const m = text.match(/\b\d{3,}\b/);
        if (!m) return say("Sorry, I'll need the order number to find it. It's in your confirmation email.");
        order = m[0];
        return verify();
      }
      case "name": {
        state = "lookup";
        await say("Thank you. One moment while I pull up the order…", 2);
        await say(
          "I see order #" + order + ": Ridgeline rain jacket, $180. I'm sorry the zipper failed. " +
          "I can offer a 20% partial refund ($36) and you keep the jacket. Would that work for you?", 8);
        state = "partial";
        return;
      }
      case "partial": {
        if (yes.test(text) && !no.test(text)) {
          state = "wrap";
          return say("Done! A $36 partial refund is on its way to your original card. Anything else I can help with?");
        }
        state = "supervisor";
        await say("I understand. Let me check with my supervisor, this may take a minute…", 2);
        await say(
          "Thanks for waiting. My supervisor approved a full refund of $180 to your original card. " +
          "We'll email a prepaid return label for the jacket. Shall I go ahead and process it?", 14);
        state = "full";
        return;
      }
      case "full": {
        if (yes.test(text) && !/\b(no|not|don't|do not)\b/i.test(text)) {
          state = "wrap";
          return say("Done! Refund RF-48213 for $180 is processed and will reach your card in 5–7 business days. " +
                     "The return label is in your email. Anything else I can help with?", 4);
        }
        return say("No problem. Let me know if you'd like me to process the full refund.");
      }
      case "wrap": {
        state = "ended";
        await say("Thanks for contacting Northwind Outfitters. Have a great day!");
        await wait(1);
        add("system", "", AGENT + " has left the chat.");
        presence.classList.remove("on");
        input.disabled = true;
        send.disabled = true;
        return;
      }
      case "ended":
        return;
      default:
        return say("One moment please, I'm still looking into it.");
    }
  }

  function verify() {
    state = "name";
    return say("Thanks! For security, can you confirm the full name on the order?");
  }

  async function drain() {
    if (busy) return;
    busy = true;
    while (queue.length) {
      // Like a person, Dana reads everything that arrived before answering.
      const text = queue.splice(0).join("\n");
      await respond(text);
    }
    busy = false;
  }

  document.getElementById("composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text || input.disabled) return;
    input.value = "";
    add("customer", "You", text);
    queue.push(text);
    drain();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      document.getElementById("composer").requestSubmit();
    }
  });

  async function connect() {
    await wait(3);
    presence.classList.add("on");
    agentName.textContent = AGENT + " · Northwind support";
    add("system", "", AGENT + " joined the chat.");
    state = "issue";
    await say("Hi, I'm " + AGENT + " from Northwind Outfitters support. How can I help today?", 1);
  }

  // A sign-in lasts for the browser session (a cookie), as on a real site: a chat opened again in
  // the same browser, after signing in there, starts straight away.
  const signedIn = document.cookie.split("; ").includes("northwind_signed_in=1");
  if (!login || signedIn) {
    connect();
  } else {
    const form = document.getElementById("signin");
    const code = document.getElementById("code");
    const err = document.getElementById("signin-error");
    log.hidden = true;
    document.getElementById("composer").hidden = true;
    agentName.textContent = "Sign in to start a chat";
    form.hidden = false;
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      if (document.getElementById("password").value !== "northwind-demo") {
        err.textContent = "Wrong email or password.";
        return;
      }
      if (login === "2fa" && code.hidden) {
        code.hidden = false;
        err.textContent = "";
        code.focus();
        return;
      }
      if (login === "2fa" && !/^\d{6}$/.test(code.value.trim())) {
        err.textContent = "That code doesn't look right.";
        return;
      }
      document.cookie = "northwind_signed_in=1; path=/; SameSite=Lax";
      form.hidden = true;
      log.hidden = false;
      document.getElementById("composer").hidden = false;
      agentName.textContent = "Connecting you to an agent…";
      connect();
    });
  }
})();
