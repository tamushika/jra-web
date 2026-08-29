"""SPEC-T71 (EV通知閾値の暫定引き下げ 1.3→1.1、T63裁定前の参考通知) の受け入れテスト。

対応する仕様: docs/codex/SPEC-T71-interim-ev-notify-threshold.md §3
  1. EV_THRESHOLD=1.1 で起動初期化した STATE["params"]["ev_threshold"] が 1.1
  2. EV_THRESHOLD 未設定→1.3、不正値 (abc)→1.3、範囲外 (0.5/5)→クランプ
  3. _send_line: EV_LINE_MIN_EV 未設定・params閾値1.1・picks EV1.15 → 送信対象になる
     (EV_LINE_MIN_EV=1.3 明示なら同条件で suppressed)
  4. 文言: 「参考情報」「閾値EV≥1.1」「暫定」を含み、「110%」「ワイドは」を含まない
     (閾値1.3のときは「暫定」を含まない)
"""
import pytest

import jra_ev


class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


def _make_alert(ev=1.15, stage=5):
    return {
        "stage": stage,
        "label": "テストレース1R 15:00発走",
        "picks": [{"num": 1, "name": "テスト馬", "ev": ev, "win_prob": 0.5,
                   "odds": "2.3", "web_value": False}],
        "wide": [],
    }


# ─── §3-1/2: EV_THRESHOLD の起動時読み込み (jra_ev._load_ev_threshold_from_env) ──

def test_ev_threshold_env_1_1_is_read_as_1_1(monkeypatch):
    monkeypatch.setenv("EV_THRESHOLD", "1.1")
    assert jra_ev._load_ev_threshold_from_env() == 1.1



def test_ev_threshold_env_unset_falls_back_to_1_3(monkeypatch):
    monkeypatch.delenv("EV_THRESHOLD", raising=False)
    assert jra_ev._load_ev_threshold_from_env() == 1.3


def test_ev_threshold_env_invalid_value_falls_back_to_1_3_with_warning(monkeypatch, capsys):
    monkeypatch.setenv("EV_THRESHOLD", "abc")
    assert jra_ev._load_ev_threshold_from_env() == 1.3
    assert "[WARN]" in capsys.readouterr().out


@pytest.mark.parametrize("raw,expected", [("0.5", 0.8), ("5", 3.0)])
def test_ev_threshold_env_out_of_range_is_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("EV_THRESHOLD", raw)
    assert jra_ev._load_ev_threshold_from_env() == expected


# ─── §3-3: _send_line の既定閾値がピック閾値に追随する ─────────────────────

def test_send_line_uses_pick_threshold_when_env_min_ev_unset(monkeypatch):
    monkeypatch.setenv("EV_LINE_CHANNEL_TOKEN", "dummy-token")
    monkeypatch.delenv("EV_LINE_MIN_EV", raising=False)
    monkeypatch.setitem(jra_ev.STATE["params"], "ev_threshold", 1.1)
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["json"] = json
        return _FakeResponse(200)

    monkeypatch.setattr(jra_ev.requests, "post", fake_post)
    status, code, err = jra_ev._send_line(_make_alert(ev=1.15))
    assert status == "sent"
    assert "json" in sent


def test_send_line_explicit_env_min_ev_overrides_pick_threshold(monkeypatch):
    monkeypatch.setenv("EV_LINE_CHANNEL_TOKEN", "dummy-token")
    monkeypatch.setenv("EV_LINE_MIN_EV", "1.3")
    monkeypatch.setitem(jra_ev.STATE["params"], "ev_threshold", 1.1)

    def fake_post(*a, **kw):
        raise AssertionError("EV_LINE_MIN_EV=1.3 明示指定なのに EV1.15 が送信された")

    monkeypatch.setattr(jra_ev.requests, "post", fake_post)
    status, code, err = jra_ev._send_line(_make_alert(ev=1.15))
    assert status == "suppressed"


# ─── §3-4: 通知文言 (参考情報・閾値・暫定の注記、旧110%文言の削除) ──────────

def test_send_line_message_contains_reference_note_and_interim_flag(monkeypatch):
    monkeypatch.setenv("EV_LINE_CHANNEL_TOKEN", "dummy-token")
    monkeypatch.delenv("EV_LINE_MIN_EV", raising=False)
    monkeypatch.setitem(jra_ev.STATE["params"], "ev_threshold", 1.1)
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["text"] = json["messages"][0]["text"]
        return _FakeResponse(200)

    monkeypatch.setattr(jra_ev.requests, "post", fake_post)
    jra_ev._send_line(_make_alert(ev=1.15))
    text = sent["text"]
    assert "参考情報" in text
    assert "閾値EV≥1.1" in text
    assert "暫定" in text
    assert "110%" not in text
    assert "ワイドは" not in text


def test_send_line_message_no_interim_flag_when_threshold_is_1_3(monkeypatch):
    monkeypatch.setenv("EV_LINE_CHANNEL_TOKEN", "dummy-token")
    monkeypatch.delenv("EV_LINE_MIN_EV", raising=False)
    monkeypatch.setitem(jra_ev.STATE["params"], "ev_threshold", 1.3)
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["text"] = json["messages"][0]["text"]
        return _FakeResponse(200)

    monkeypatch.setattr(jra_ev.requests, "post", fake_post)
    jra_ev._send_line(_make_alert(ev=1.35))
    text = sent["text"]
    assert "参考情報" in text
    assert "閾値EV≥1.3" in text
    assert "暫定" not in text


def test_notice_lines_helper_matches_spec_wording():
    assert jra_ev._notice_lines(1.1) == [
        "※参考情報です (購入推奨ではありません)・閾値EV≥1.1",
        "※T63裁定前の暫定閾値",
    ]
    assert jra_ev._notice_lines(1.3) == [
        "※参考情報です (購入推奨ではありません)・閾値EV≥1.3",
    ]
