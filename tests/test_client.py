"""Tests for app.linkedin.client classification, headers, and rate limiter.

Pure offline tests for:
  - classify() with synthetic responses (the 200-with-HTML-login trap, etc.)
  - build_headers() (CSRF gotcha: quotes stripped in csrf-token, present in cookie)
  - TokenBucket and jittered_delay_ms

No curl_cffi import is exercised; _do_fetch is not called.
"""

from __future__ import annotations

import pytest

from app.linkedin.client import CHROME_UA, Outcome, build_headers, classify
from app.linkedin.session import Session
from app.ratelimit import TokenBucket, jittered_delay_ms


class TestClassify:
    @pytest.mark.parametrize(
        "status, ct, first_byte, final_url, expected",
        [
            # Clean JSON success
            (200, "application/json", ord("{"), "https://www.linkedin.com/voyager/api/x", Outcome.OK),
            # NOT_FOUND
            (404, "application/json", ord("{"), "https://www.linkedin.com/voyager/api/x", Outcome.NOT_FOUND),
            # AUTH_EXPIRED via 401
            (401, "application/json", ord("{"), "https://www.linkedin.com/voyager/api/x", Outcome.AUTH_EXPIRED),
            # AUTH_EXPIRED via 403 (CSRF mismatch)
            (403, "application/json", ord("{"), "https://www.linkedin.com/voyager/api/x", Outcome.AUTH_EXPIRED),
            # CHALLENGE via 999
            (999, "text/html", ord("<"), "https://www.linkedin.com/voyager/api/x", Outcome.CHALLENGE),
            # CHALLENGE via redirect to /checkpoint/challenge
            (200, "text/html", ord("<"), "https://www.linkedin.com/checkpoint/challenge/ABC", Outcome.CHALLENGE),
            # AUTH_EXPIRED via redirect to /uas/login
            (200, "text/html", ord("<"), "https://www.linkedin.com/uas/login", Outcome.AUTH_EXPIRED),
            # RATE_LIMITED
            (429, "application/json", ord("{"), "https://www.linkedin.com/voyager/api/x", Outcome.RATE_LIMITED),
            # SERVER_ERROR 500
            (500, "text/html", ord("<"), "https://www.linkedin.com/voyager/api/x", Outcome.SERVER_ERROR),
            # SERVER_ERROR 503
            (503, "application/json", ord("{"), "https://www.linkedin.com/voyager/api/x", Outcome.SERVER_ERROR),
            # THE TRAP: 200 with HTML body (login wall) -> AUTH_EXPIRED, not OK
            (200, "text/html", ord("<"), "https://www.linkedin.com/voyager/api/x", Outcome.AUTH_EXPIRED),
            # 200 with JSON content-type but HTML body -> AUTH_EXPIRED
            (200, "application/json", ord("<"), "https://www.linkedin.com/voyager/api/x", Outcome.AUTH_EXPIRED),
            # 200 with non-JSON non-HTML content -> UNPARSEABLE
            (200, "application/octet-stream", 0x89, "https://www.linkedin.com/voyager/api/x", Outcome.UNPARSEABLE),
            # 200 with empty content-type and JSON body -> OK (some servers omit ct)
            (200, "", ord("{"), "https://www.linkedin.com/voyager/api/x", Outcome.UNPARSEABLE),
        ],
    )
    def test_classify(self, status, ct, first_byte, final_url, expected) -> None:
        assert classify(status, ct, first_byte, final_url) is expected

    def test_ok_requires_json_content_type_and_non_html_body(self) -> None:
        # Both conditions must hold for OK.
        assert classify(200, "application/json", ord("{"), "https://x/voyager/api/x") is Outcome.OK
        assert classify(200, "text/html", ord("{"), "https://x/voyager/api/x") is Outcome.AUTH_EXPIRED
        assert classify(200, "application/json", ord("<"), "https://x/voyager/api/x") is Outcome.AUTH_EXPIRED


class TestBuildHeaders:
    def test_csrf_token_strips_quotes(self) -> None:
        # jsessionid stored WITHOUT quotes in config.
        s = Session(li_at="tok", jsessionid="ajax:123")
        h = build_headers(s)
        # csrf-token has the bare value (no quotes).
        assert h["csrf-token"] == "ajax:123"
        # cookie has the value WITH surrounding quotes.
        assert 'JSESSIONID="ajax:123"' in h["cookie"]
        assert "li_at=tok" in h["cookie"]

    def test_accept_header_is_normalized_envelope(self) -> None:
        s = Session(li_at="tok", jsessionid="ajax:123")
        h = build_headers(s)
        assert h["accept"] == "application/vnd.linkedin.normalized+json+2.1"

    def test_restli_protocol_set(self) -> None:
        s = Session(li_at="tok", jsessionid="ajax:123")
        h = build_headers(s)
        assert h["x-restli-protocol-version"] == "2.0.0"

    def test_user_agent_is_chrome(self) -> None:
        s = Session(li_at="tok", jsessionid="ajax:123")
        h = build_headers(s)
        assert h["user-agent"] == CHROME_UA
        assert "Chrome" in h["user-agent"]

    def test_referer_set(self) -> None:
        s = Session(li_at="tok", jsessionid="ajax:123")
        h = build_headers(s)
        assert h["referer"] == "https://www.linkedin.com/feed/"


class TestTokenBucket:
    def test_consume_within_capacity(self) -> None:
        clk = [0.0]
        b = TokenBucket(rate=1.0, capacity=3, clock=lambda: clk[0])
        assert b.consume(1)
        assert b.consume(1)
        assert b.consume(1)
        # capacity exhausted, no time passed
        assert not b.consume(1)

    def test_refill_over_time(self) -> None:
        clk = [0.0]
        b = TokenBucket(rate=2.0, capacity=2, clock=lambda: clk[0])
        assert b.consume(2)
        assert not b.consume(1)
        clk[0] = 1.0  # 1s at rate 2 -> 2 tokens
        assert b.consume(1)
        assert b.consume(1)
        assert not b.consume(1)

    def test_refill_capped_at_capacity(self) -> None:
        clk = [0.0]
        b = TokenBucket(rate=10.0, capacity=2, clock=lambda: clk[0])
        clk[0] = 100.0  # huge elapsed time, but capacity caps refill
        assert b.consume(2)
        assert not b.consume(1)


class TestJitter:
    def test_jitter_within_range(self) -> None:
        for _ in range(100):
            d = jittered_delay_ms(800, 2500)
            assert 800 <= d <= 2500

    def test_jitter_swaps_if_min_gt_max(self) -> None:
        d = jittered_delay_ms(2500, 800)
        assert 800 <= d <= 2500