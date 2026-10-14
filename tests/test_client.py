import json
from datetime import date

import pytest
import requests

from mfp_connector.client import AuthExpired, Blocked, MFPClient, UpstreamError, check_response


def resp(status=200, body="", ctype="application/json", headers=None):
    r = requests.Response()
    r.status_code = status
    r._content = (body if isinstance(body, str) else json.dumps(body)).encode()
    r.headers.update({"content-type": ctype, **(headers or {})})
    return r


def test_json_ok():
    assert check_response(resp(body={"a": 1}), "x") == {"a": 1}


@pytest.mark.parametrize(
    "r,exc",
    [
        # what MFP actually returns with no/invalid session (seen in phase 0)
        (resp(400, {"error": "validation/3", "error_description": "Missing HTTP header: Authorization"}), AuthExpired),
        (resp(302, headers={"location": "https://www.myfitnesspal.com/account/login"}), AuthExpired),
        (resp(401, {}), AuthExpired),
        (resp(200, "<html>Log In to MyFitnessPal</html>", "text/html"), AuthExpired),
        (resp(403, "<title>Just a moment...</title>", "text/html"), Blocked),
        (resp(503, "", "text/html", {"cf-mitigated": "challenge"}), Blocked),
        (resp(500, {"error": "boom"}), UpstreamError),
        (resp(200, "<html>maintenance</html>", "text/html"), UpstreamError),
        (resp(200, "{not json"), UpstreamError),
    ],
)
def test_classification(r, exc):
    with pytest.raises(exc):
        check_response(r, "GET /x")


def test_food_named_just_a_moment_is_not_a_challenge():
    assert check_response(resp(body=[{"description": "Just a moment bar"}]), "x")


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(responses):
    s = FakeSession(responses)
    return MFPClient("c=1", "UA", "someone", session=s, sleep=lambda _: None), s


def test_retries_5xx_and_network_errors_then_succeeds():
    c, s = client([requests.ConnectionError("x"), resp(502, {}), resp(body=[])])
    assert c.diary_report(date(2026, 1, 1), date(2026, 1, 2)) == []
    assert len(s.calls) == 3


def test_auth_errors_not_retried():
    c, s = client([resp(401, {})])
    with pytest.raises(AuthExpired):
        c.nutrient_goals(date(2026, 1, 1))
    assert len(s.calls) == 1


def test_requests_carry_auth_and_no_redirects():
    c, s = client([resp(body=[])])
    c.diary_report(date(2026, 1, 1), date(2026, 1, 31))
    method, url, kw = s.calls[0]
    assert method == "POST" and url.endswith("/api/services/diary/report")
    assert kw["headers"]["Cookie"] == "c=1" and kw["headers"]["User-Agent"] == "UA"
    assert kw["allow_redirects"] is False
    assert kw["json"]["from"] == "2026-01-01" and kw["json"]["to"] == "2026-01-31"
    assert kw["json"]["username"] == "someone"


def test_measurements_paginate():
    page1 = {"items": [{"id": str(i)} for i in range(200)], "has_more": True}
    page2 = {"items": [{"id": "last"}], "has_more": False}
    c, s = client([resp(body=page1), resp(body=page2)])
    assert len(c.measurements()) == 201
    assert [call[2]["params"]["offset"] for call in s.calls] == [0, 200]
