"""A pre-chat form is filled only from the shareable details, and only where a detail fits a field.
Also, without a browser: which button names open a chat and which refuse a cookie banner."""

import pytest

from support_chat_plugin import browser, prechat

NAME = {"label": "Name", "type": "text", "required": True, "options": []}
EMAIL = {"label": "Email", "type": "email", "required": True, "options": []}
ORDER = {"label": "Order number", "type": "text", "required": False, "options": []}


def test_name_and_email_from_the_shareable_details():
    values, missing = prechat.plan([NAME, EMAIL, ORDER],
                                   {"full name": "Sam Rivera", "e-mail": "sam@example.com", "order #": "1234"})
    assert values == {"Name": "Sam Rivera", "Email": "sam@example.com", "Order number": "1234"}
    assert missing == []


def test_a_required_field_nothing_fits_is_missing():
    values, missing = prechat.plan([NAME, EMAIL, ORDER], {"name": "Sam Rivera"})
    assert values == {"Name": "Sam Rivera"}
    assert missing == ["Email"]


def test_an_optional_field_nothing_fits_is_left_empty():
    values, missing = prechat.plan([NAME, ORDER], {"your name": "Sam Rivera"})
    assert "Order number" not in values and missing == []


def test_field_types_check_the_value():
    phone = {"label": "Phone", "type": "tel", "required": True, "options": []}
    values, missing = prechat.plan([EMAIL, phone], {"email": "sam at example", "mobile": "12-34"})
    assert values == {} and missing == ["Email", "Phone"]
    values, _ = prechat.plan([EMAIL, phone], {"mail": "sam@example.com", "telephone": "+1 (555) 010-2030"})
    assert values == {"Email": "sam@example.com", "Phone": "+1 (555) 010-2030"}


def test_a_last_name_field_takes_only_a_last_name():
    last = {"label": "Last name", "type": "text", "required": True, "options": []}
    first = {"label": "First name", "type": "text", "required": True, "options": []}
    assert prechat.plan([first, last], {"name": "Sam"}) == ({"First name": "Sam"}, ["Last name"])
    assert prechat.plan([first, last], {"name": "Sam", "surname": "Rivera"})[0] == {"First name": "Sam", "Last name": "Rivera"}


def test_a_select_takes_only_one_of_its_options():
    dept = {"label": "Department", "type": "select", "required": True, "options": ["Sales", "Support"]}
    assert prechat.plan([dept], {"topic": "support"}) == ({"Department": "support"}, [])
    assert prechat.plan([dept], {"topic": "A broken zipper"}) == ({}, ["Department (one of: Sales, Support)"])


def test_a_checkbox_is_ticked_only_by_a_yes():
    box = {"label": "Email me a transcript", "type": "checkbox", "required": False, "options": []}
    assert prechat.plan([box], {"email me a transcript": "yes"})[0] == {"Email me a transcript": "yes"}
    assert prechat.plan([box], {"email me a transcript": "sam@example.com"})[0] == {}
    terms = {"label": "I agree to the privacy policy", "type": "checkbox", "required": True, "options": []}
    assert prechat.plan([terms], {"name": "Sam"}) == ({}, ["I agree to the privacy policy"])


def test_one_value_fills_one_field():
    other = {"label": "Full name", "type": "text", "required": True, "options": []}
    values, missing = prechat.plan([NAME, other], {"name": "Sam Rivera"})
    assert values == {"Name": "Sam Rivera"} and missing == ["Full name"]
    twice = prechat.plan([NAME, dict(NAME)], {"name": "Sam Rivera"})
    assert twice == ({"Name": "Sam Rivera"}, [])


def test_a_company_name_is_not_the_users_name():
    company = {"label": "Company name", "type": "text", "required": False, "options": []}
    assert prechat.plan([company], {"name": "Sam Rivera"}) == ({}, [])


@pytest.mark.parametrize("name, opens", [
    ("Chat with us", True), ("Open chat", True), ("Open messaging window", True), ("Open Intercom Messenger", True),
    ("Open LiveChat chat widget", True), ("Get help", True), ("Chat", True), ("Close chat", False),
    ("Minimize chat", False), ("End chat", False), ("Hide chat", False), ("Email transcript", False),
    ("Download chat transcript", False), ("Chat history", False), ("Leave a message", False), ("Send", False),
    ("Weekend chat hours are listed on our help center page for every region", False),
    ("Live chat", True), ("Livechat", True), ("Snapchat", False), ("WeChat", False),
])
def test_launcher_names(name, opens):
    assert bool(browser.LAUNCHER.search(name)) is opens


@pytest.mark.parametrize("name, refuses", [
    ("Reject all", True), ("Decline", True), ("Deny", True), ("Necessary only", True), ("Only necessary", True),
    ("Use necessary cookies only", True), ("Essential cookies only", True), ("Accept all", False),
    ("Agree", False), ("Allow all", False), ("Reject all and close", False), ("Manage preferences", False),
])
def test_a_cookie_banner_is_only_ever_refused(name, refuses):
    assert bool(browser.REFUSE_CONSENT.search(name)) is refuses
