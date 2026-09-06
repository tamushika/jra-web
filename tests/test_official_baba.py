"""不具合1: 当日馬場状態が公式値と食い違う問題への対応テスト。

出馬表ページ (accessD) の `div.cell.baba` (JRAがレース当日に随時更新する公式値) を
`parse_official_baba` で読み取り、`analyze_race_url` がそれを含水率ページ
(`fetch_baba_info`、朝5:30測定のまま更新されない) より優先することを確認する。
"""
from pathlib import Path
import sys

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "api"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(API_DIR))

import index as api_index  # noqa: E402

FLAT_FIXTURE = (
    ROOT / "tests" / "fixtures" / "t48"
    / "flat_race_20260726_chukyo6_jump_returner.html"
)
# venue=07(中京), race=06 を埋め込んだダミーCNAME (v_code/year の正規表現要件を満たす)
FLAT_URL = (
    "https://www.jra.go.jp/JRADB/accessD.html?"
    "CNAME=pw01dde060720260726010101%2FCA"
)


class _Response:
    def __init__(self, content):
        self.content = content
        self.encoding = None

    @property
    def text(self):
        return self.content.decode(self.encoding or "cp932", errors="replace")


def _soup(html_fragment):
    return BeautifulSoup(html_fragment, "html.parser")


def test_parse_official_baba_turf_only():
    soup = _soup(
        '<div class="cell baba"><ul>'
        '<li class="weather"><span class="inner"><span class="cap">天候</span>'
        '<span class="txt">雨</span></span></li>'
        '<li class="turf"><span class="inner"><span class="cap">芝</span>'
        '<span class="txt">重</span></span></li>'
        '</ul></div>'
    )
    result = api_index.parse_official_baba(soup)
    assert result == {"weather": "雨", "turf": "重", "dirt": None}


def test_parse_official_baba_dirt_only_uses_durt_class():
    soup = _soup(
        '<div class="cell baba"><ul>'
        '<li class="weather"><span class="inner"><span class="cap">天候</span>'
        '<span class="txt">雨</span></span></li>'
        '<li class="durt"><span class="inner"><span class="cap">ダート</span>'
        '<span class="txt">不良</span></span></li>'
        '</ul></div>'
    )
    result = api_index.parse_official_baba(soup)
    assert result == {"weather": "雨", "turf": None, "dirt": "不良"}


def test_parse_official_baba_accepts_dirt_class_spelling_too():
    # JRAの実ページは li.durt だが、念のため li.dirt (英語綴り) もフォールバックで読む
    soup = _soup(
        '<div class="cell baba"><ul>'
        '<li class="dirt"><span class="inner"><span class="cap">ダート</span>'
        '<span class="txt">稍重</span></span></li>'
        '</ul></div>'
    )
    result = api_index.parse_official_baba(soup)
    assert result["dirt"] == "稍重"


def test_parse_official_baba_missing_element_returns_all_none():
    soup = _soup("<html><body><p>no baba info here</p></body></html>")
    result = api_index.parse_official_baba(soup)
    assert result == {"weather": None, "turf": None, "dirt": None}


def test_parse_official_baba_rejects_unexpected_txt_value():
    # 想定外の文字列 (発表前のプレースホルダ等) は採用しない
    soup = _soup(
        '<div class="cell baba"><ul>'
        '<li class="turf"><span class="inner"><span class="cap">芝</span>'
        '<span class="txt">未定</span></span></li>'
        '</ul></div>'
    )
    result = api_index.parse_official_baba(soup)
    assert result["turf"] is None


def test_analyze_race_url_prefers_official_baba_over_moisture_page(monkeypatch):
    raw = FLAT_FIXTURE.read_bytes()
    monkeypatch.setattr(api_index.requests, "get", lambda *_a, **_kw: _Response(raw))
    monkeypatch.setattr(api_index, "fetch_rail_settings", lambda: {})
    # 含水率ページ由来は「芝: 稍重」に固定 (公式値と食い違わせる)
    monkeypatch.setattr(
        api_index, "fetch_baba_info",
        lambda venue: f"馬場情報 [{venue}] 芝: 稍重 (水分: G前10% 4角10% クッション値: 9.0) / ダート: 良 (水分: G前5% 4角5%)",
    )

    result = api_index.analyze_race_url(FLAT_URL)

    assert result["success"] is True
    assert result["race_type"] == "芝"
    # フィクスチャの公式値 (div.cell.baba: 天候 晴 / 芝 良) が優先される
    assert result["baba_cond"] == "良"
    assert result["baba_official"] == {
        "weather": "晴", "turf": "良", "dirt": None, "source": "race_page",
    }
    # 画面表示用の baba_info には公式値が先頭に付き、含水率側の文字列も後ろに残る
    assert result["baba_info"].startswith("当日公式: 天候 晴 / 芝 良 ｜ ")
    assert "稍重" in result["baba_info"]


def test_analyze_race_url_falls_back_to_moisture_page_when_no_official_baba(monkeypatch):
    raw = FLAT_FIXTURE.read_bytes()
    # div.cell.baba を空にした版 (未発表 or 終了レース相当) を用意する
    html = raw.decode("cp932", errors="replace")
    html_no_official = html.replace(
        '<li class="weather"><span class="inner"><span class="cap">天候</span>'
        '<span class="txt">晴</span></span></li>'
        '<li class="turf"><span class="inner"><span class="cap">芝</span>'
        '<span class="txt">良</span></span></li>',
        "",
    )
    assert html_no_official != html  # 前提: 置換が実際に効いていること
    patched_raw = html_no_official.encode("cp932", errors="replace")

    monkeypatch.setattr(api_index.requests, "get", lambda *_a, **_kw: _Response(patched_raw))
    monkeypatch.setattr(api_index, "fetch_rail_settings", lambda: {})
    monkeypatch.setattr(
        api_index, "fetch_baba_info",
        lambda venue: f"馬場情報 [{venue}] 芝: 稍重 (水分: G前10% 4角10% クッション値: 9.0) / ダート: 良 (水分: G前5% 4角5%)",
    )

    result = api_index.analyze_race_url(FLAT_URL)

    assert result["success"] is True
    assert result["baba_official"] == {
        "weather": None, "turf": None, "dirt": None, "source": "moisture_page",
    }
    assert result["baba_cond"] == "稍"
    assert result["baba_info"].startswith("馬場情報 [中京] 芝: 稍重")
    assert "当日公式" not in result["baba_info"]
