from __future__ import annotations

import io
import json
from urllib.error import HTTPError

import pytest

from chan520_skill import data


class Response:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.body


def test_tencent_kline_payload_falls_back_between_hosts(monkeypatch):
    requested: list[str] = []

    def fake_urlopen(request, timeout):
        _ = timeout
        requested.append(request.full_url)
        if len(requested) == 1:
            raise HTTPError(request.full_url, 501, "WAF", {}, io.BytesIO())
        return Response({"code": 0, "data": {}})

    monkeypatch.setattr(data, "urlopen", fake_urlopen)

    payload = data.tencent_kline_payload("sz000429,day,2026-01-01,2026-07-22,640,qfq", timeout=5)

    assert payload["code"] == 0
    assert len(requested) == 2
    assert requested[0].startswith(data.TENCENT_KLINE_ENDPOINTS[0])
    assert requested[1].startswith(data.TENCENT_KLINE_ENDPOINTS[1])


def test_sina_history_requires_and_parses_exact_date(monkeypatch):
    monkeypatch.setattr(
        data,
        "urlopen",
        lambda _request, timeout: Response(
            [
                {"day": "2026-07-21", "open": "10.0", "high": "10.5", "low": "9.8", "close": "10.2", "volume": "100"},
                {"day": "2026-07-22", "open": "10.1", "high": "10.8", "low": "10.0", "close": "10.6", "volume": "120"},
            ]
        ),
    )

    _meta, rows = data.sina_history("000429", data.date(2026, 7, 22), timeout=5)

    assert rows[-1].date == data.date(2026, 7, 22)
    assert rows[-1].close == 10.6
    assert rows[-1].pct_chg == pytest.approx((10.6 / 10.2 - 1) * 100)
