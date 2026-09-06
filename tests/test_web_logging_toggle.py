"""不具合2: 本番Web (Vercel) で予想ログ保存が毎回失敗する問題への対応テスト。

Vercel環境 (/var/task が読み取り取り専用) では JRA_LOG_DB を明示指定しない限り
log_race_prediction の呼び出し自体をスキップし、logging_warning ではなく
logging_skipped を返すことを確認する。
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "api"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(API_DIR))

import index as api_index  # noqa: E402


def test_logging_enabled_when_jra_log_db_set_even_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("JRA_LOG_DB", "/tmp/x.db")
    assert api_index._prediction_logging_enabled() is True


def test_logging_disabled_on_vercel_without_jra_log_db(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("JRA_LOG_DB", raising=False)
    assert api_index._prediction_logging_enabled() is False


def test_logging_enabled_when_neither_env_var_set(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("JRA_LOG_DB", raising=False)
    assert api_index._prediction_logging_enabled() is True


def test_logging_enabled_with_jra_log_db_and_no_vercel(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.setenv("JRA_LOG_DB", "/tmp/x.db")
    assert api_index._prediction_logging_enabled() is True


def _fixed_result():
    return {
        "success": True,
        "race_info": "dummy",
        "race_name": "dummy",
        "race_date": "20260101",
        "race_num": 1,
        "venue": "中山",
        "race_type": "芝",
        "dist_val": 2000,
        "race_class": "オープン",
        "baba_cond": "良",
        "baba_info": "dummy",
        "horses": [],
        "matrix_data": [],
        "analysis_excluded": False,
    }


def test_scrape_endpoint_skips_logging_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.delenv("JRA_LOG_DB", raising=False)
    monkeypatch.setattr(api_index, "analyze_race_url", lambda url, mode="簡易": _fixed_result())

    called = {"count": 0}

    def _fake_log(*args, **kwargs):
        called["count"] += 1

    monkeypatch.setattr(api_index, "log_race_prediction", _fake_log)

    client = api_index.app.test_client()
    resp = client.post("/api/scrape", json={"url": "https://example.com/race"})
    data = resp.get_json()

    assert resp.status_code == 200
    assert data["logging_skipped"] == "vercel_readonly"
    assert "logging_warning" not in data
    assert called["count"] == 0


def test_scrape_endpoint_logs_when_not_on_vercel(monkeypatch):
    monkeypatch.delenv("VERCEL", raising=False)
    monkeypatch.delenv("JRA_LOG_DB", raising=False)
    monkeypatch.setattr(api_index, "analyze_race_url", lambda url, mode="簡易": _fixed_result())

    called = {"count": 0}

    def _fake_log(*args, **kwargs):
        called["count"] += 1

    monkeypatch.setattr(api_index, "log_race_prediction", _fake_log)

    client = api_index.app.test_client()
    resp = client.post("/api/scrape", json={"url": "https://example.com/race"})
    data = resp.get_json()

    assert resp.status_code == 200
    assert "logging_skipped" not in data
    assert "logging_warning" not in data
    assert called["count"] == 1
