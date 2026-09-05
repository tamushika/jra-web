"""WIN5「対象レースを取得」URL探索の2段目シード発見 (2026-09-05):

JRAトップページには一部会場 (例: 阪神11R) のaccessDリンクしか無い日があり、
中山など他会場のシードが0本になって該当レースが探索対象から漏れる
(実例: 2026-09-05の中山10R/11R)。トップで得たシードURL自体を辿り、その
ナビゲーションリンクから (会場, 日付) ごとの代表URLを追加収集することで
解決する。
"""
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "api"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(API_DIR))

import index as api_index  # noqa: E402


class _Response:
    def __init__(self, content):
        self.content = content
        self.encoding = None
        self.status_code = 200

    @property
    def text(self):
        return self.content.decode(self.encoding or "cp932", errors="replace")


def _make_fake_get(url_to_html):
    """url -> html(str) の対応表で応答する requests.get の偽物。呼び出し回数を数える。"""
    calls = {}

    def fake_get(url, *_args, **_kwargs):
        calls[url] = calls.get(url, 0) + 1
        html = url_to_html.get(url, "")
        return _Response(html.encode("cp932", errors="replace"))

    return fake_get, calls


TOP_URL = "https://www.jra.go.jp/"
HANSHIN_URL = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=pw01dde0109202604011120260905/72"
NAKAYAMA_0905_URL = "https://www.jra.go.jp/JRADB/accessD.html?CNAME=pw01dde0106202604011120260905/94"

TOP_HTML = f'<a href="/JRADB/accessD.html?CNAME=pw01dde0109202604011120260905/72">阪神11R</a>'

# 阪神11Rのページには自分自身への出馬表リンクに加えて、他会場・他日への
# ナビゲーションリンク (4回中山1日=9/5代表, 4回中山2日=9/6) がある。
HANSHIN_HTML = (
    '<a href="/JRADB/accessD.html?CNAME=pw01dde0109202604011120260905/72">阪神11R</a>'
    '<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604011120260905/94">4回中山1日</a>'
    '<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604021120260906/C4">4回中山2日</a>'
)

# 中山9/5の代表ページには1R〜12R (一部抜粋) のリンクがあり、9/6のリンクも1本混じる。
NAKAYAMA_0905_HTML = (
    '<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604010120260905/AA">1R</a>'
    '<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604011020260905/DF">10R</a>'
    '<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604011120260905/94">11R</a>'
    '<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604011220260905/BB">12R</a>'
    '<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604020120260906/CC">翌日1R</a>'
)


def test_stage2_seed_discovery_resolves_nakayama_via_hanshin_top_link(monkeypatch):
    """(a) トップに中山のシードが無くても、阪神ページ経由の代表リンクから
    中山10R/11Rが解決され、阪神11Rも従来どおり解決される。"""
    fake_get, _calls = _make_fake_get({
        TOP_URL: TOP_HTML,
        HANSHIN_URL: HANSHIN_HTML,
        NAKAYAMA_0905_URL: NAKAYAMA_0905_HTML,
    })
    monkeypatch.setattr(api_index.requests, "get", fake_get)

    races = [
        {"idx": 0, "venue": "中山", "race_num": 10},
        {"idx": 1, "venue": "中山", "race_num": 11},
        {"idx": 2, "venue": "阪神", "race_num": 11},
    ]
    result = api_index._find_win5_urls(races, "20260905")

    assert len(result) == 3
    assert result[0] == (
        "https://www.jra.go.jp/JRADB/accessD.html?CNAME=pw01dde0106202604011020260905/DF"
    )
    assert result[1] == NAKAYAMA_0905_URL
    assert result[2] == HANSHIN_URL


def test_stage2_seed_discovery_ignores_representative_link_for_other_date(monkeypatch):
    """(b) 阪神ページに中山9/6の代表リンクしか無い場合、target_date(9/5)とは
    一致しないので中山は未解決のまま (resultに入らない)。"""
    hanshin_html_only_next_day = (
        '<a href="/JRADB/accessD.html?CNAME=pw01dde0109202604011120260905/72">阪神11R</a>'
        '<a href="/JRADB/accessD.html?CNAME=pw01dde0106202604021020260906/CC">4回中山2日</a>'
    )
    fake_get, _calls = _make_fake_get({
        TOP_URL: TOP_HTML,
        HANSHIN_URL: hanshin_html_only_next_day,
    })
    monkeypatch.setattr(api_index.requests, "get", fake_get)

    races = [{"idx": 0, "venue": "中山", "race_num": 10}]
    result = api_index._find_win5_urls(races, "20260905")

    assert 0 not in result
    assert result == {}


def test_stage2_seed_pages_are_fetched_only_once_each(monkeypatch):
    """(c) 中山10R/11Rは同一の代表ページから解決されるため、
    トップ・阪神ページ・中山ページはそれぞれ1回しか取得されない。"""
    fake_get, calls = _make_fake_get({
        TOP_URL: TOP_HTML,
        HANSHIN_URL: HANSHIN_HTML,
        NAKAYAMA_0905_URL: NAKAYAMA_0905_HTML,
    })
    monkeypatch.setattr(api_index.requests, "get", fake_get)

    races = [
        {"idx": 0, "venue": "中山", "race_num": 10},
        {"idx": 1, "venue": "中山", "race_num": 11},
        {"idx": 2, "venue": "阪神", "race_num": 11},
    ]
    result = api_index._find_win5_urls(races, "20260905")

    assert len(result) == 3
    assert calls.get(TOP_URL) == 1
    assert calls.get(HANSHIN_URL) == 1
    assert calls.get(NAKAYAMA_0905_URL) == 1
