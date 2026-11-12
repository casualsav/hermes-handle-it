import json

from support_chat_plugin import handlers


class Ctx:
    """Hermes's PluginContext: dispatch_tool runs the named core tool with the kwargs given."""

    def __init__(self):
        self.dispatched = []

    def dispatch_tool(self, name, args, **kwargs):
        self.dispatched.append((name, args))
        reply = kwargs["callback"]([{"qid": "q0", **args["questions"][0], "multi_select": False}])
        return json.dumps({"responses": [{"status": "answered", "user_response": reply["answers"]["q0"]}],
                           "outcome": "answered"})


class User:
    def clarify_callback(self, questions):
        return {"answers": {"q0": questions[0]["choices"][1]}, "outcome": "answered"}


def test_the_approval_question_goes_through_hermess_clarify_tool(monkeypatch):
    ctx = Ctx()
    monkeypatch.setattr(handlers, "_ctx", ctx)

    answer = handlers._asker(User())("Take the $20 credit?", ["Accept", "Decline"])

    assert answer == "Decline"
    assert ctx.dispatched == [("clarify", {"questions": [{"question": "Take the $20 credit?",
                                                          "choices": ["Accept", "Decline"]}]})]
