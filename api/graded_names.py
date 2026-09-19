"""SPEC-T79 §2.1: 重賞レース名の正規化・突合 (純関数のみ、副作用なし)。

3つの由来のレース名表記を同じ `race_key` に束ねる:
  - TARGET由来 (ability.db 1986〜2025, race_class='重賞'): 略称+ (Ｈ) + G1/G2/G3
    (例 "菊花賞G1", "アルゼンＨG2", "みやこＳG3")
  - netkeiba由来 (ability.db 2026, race_class='オープン'): 正式名
    (例 "第75回日刊スポ賞中山金杯(GIII)")
  - 当日出馬表 (jra_ev の race_info): 冠 + 空白 + レース名、格の表記なし
    (例 "農林水産省賞典 新潟記念")

正規化は全て `str.replace`/`re` ベースの純粋な文字列処理で、DB・ネットワークには
一切アクセスしない (build_graded_cache.py / jra_graded.py から呼ばれる)。
"""
import json
import os
import re
import unicodedata

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SPONSORS_PATH = os.path.join(BASE_DIR, "data", "graded_sponsors.json")
DEFAULT_ALIASES_PATH = os.path.join(BASE_DIR, "data", "graded_aliases.json")

# race_key() で使う「正式名称→TARGET式1文字略称」変換 (全角1文字)。
# 変換後に NFKC で半角化するため、最終的な略称は半角英字になる
# (例 "ステークス"→"Ｓ"→NFKC→"S")。TARGET生データの "Ｓ/Ｈ/Ｃ/Ｔ" も
# normalize_race_name() のNFKC正規化で同じ半角形になるため、両者は一致する。
_KEY_WORD_REPLACEMENTS = (
    ("ステークス", "Ｓ"),
    ("カップ", "Ｃ"),
    ("トロフィー", "Ｔ"),
)

_LEADING_KAISUU_RE = re.compile(r"^第[0-9]+回\s*")
_GRADE_PAREN_RE = re.compile(r"\((GI{1,3})\)\s*$")
_GRADE_SUFFIX_RE = re.compile(r"(G[123])\s*$")
_TRAILING_H_RE = re.compile(r"[HＨ]\s*$")
_SPONSOR_PATTERN_RE = re.compile(r"^.+?(賞典|賞|杯)")

_GRADE_PAREN_MAP = {"GI": "G1", "GII": "G2", "GIII": "G3"}

# JRA重賞の障害 (ジャンプ) 表記: TARGET由来はグレード表記の直前に "J" が付く
# (例 "中山大障JG1", "阪神スプJG2", "小倉サマHJG3")。"JBCレデG1" のように
# 単に頭文字がJであるだけの平地重賞と区別するため、末尾のみを見る。
_JUMP_GRADE_SUFFIX_RE = re.compile(r"H?JG[123]\s*$")


def is_jump_race(race_name, track_type=None):
    """障害(ジャンプ)重賞かどうかを判定する (SPEC-T79 §2.2-2)。"""
    name = str(race_name or "")
    if "障害" in name or "ジャンプ" in name or "障" in name:
        return True
    if _JUMP_GRADE_SUFFIX_RE.search(name):
        return True
    if track_type is not None and track_type not in ("芝", "ダート"):
        return True
    return False


def normalize_race_name(name):
    """レース名を (base, grade) に正規化する。grade は 'G1'/'G2'/'G3' または None。

    手順: NFKC正規化 → 先頭の「第N回」除去 → 末尾の (GI|GII|GIII) または G[123] を
    格として抽出して除去 → 末尾のＨ/H (ハンデ印) を除去 → 前後空白除去。
    """
    if name is None:
        return "", None
    s = unicodedata.normalize("NFKC", str(name)).strip()
    s = _LEADING_KAISUU_RE.sub("", s).strip()

    grade = None
    m = _GRADE_PAREN_RE.search(s)
    if m:
        grade = _GRADE_PAREN_MAP.get(m.group(1))
        s = s[: m.start()].rstrip()
    else:
        m2 = _GRADE_SUFFIX_RE.search(s)
        if m2:
            grade = m2.group(1)
            s = s[: m2.start()].rstrip()

    s = _TRAILING_H_RE.sub("", s).rstrip()
    s = unicodedata.normalize("NFKC", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s, grade


def race_key(base):
    """正規化済みbaseから4文字キーを作る (4文字未満はそのまま)。"""
    s = str(base or "")
    for word, abbr in _KEY_WORD_REPLACEMENTS:
        s = s.replace(word, abbr)
    s = unicodedata.normalize("NFKC", s)
    return s[:4] if len(s) > 4 else s


def _load_json_list_or_dict(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_sponsors(path=None):
    """data/graded_sponsors.json (冠リスト、配列) を読む。無ければ空配列。"""
    return _load_json_list_or_dict(path or DEFAULT_SPONSORS_PATH, [])


def load_aliases(path=None):
    """data/graded_aliases.json ({表記: key}) を読む。無ければ空dict。"""
    return _load_json_list_or_dict(path or DEFAULT_ALIASES_PATH, {})


def _known_key_set(known_keys):
    """known_keys (set/list/dict いずれでも可) から有効なキー集合を作る。"""
    if isinstance(known_keys, dict):
        return set(known_keys.keys())
    return set(known_keys or ())


def _condition_of(known_keys, key):
    """known_keys が dict (key -> {'place':,'track_type':,'distance':}) の場合、
    そのキーの直近開催条件を返す。単なる集合/リストの場合は None。"""
    if isinstance(known_keys, dict):
        return known_keys.get(key)
    return None


def _matches_condition(cond, place, track_type, distance):
    if not cond:
        return False
    ok = True
    if place is not None and cond.get("place") is not None:
        ok = ok and (cond.get("place") == place)
    if track_type is not None and cond.get("track_type") is not None:
        ok = ok and (cond.get("track_type") == track_type)
    if distance is not None and cond.get("distance") is not None:
        ok = ok and (cond.get("distance") == distance)
    return ok


def _pick_best(candidates, known_keys, place, track_type, distance):
    """複数の (text, how) 候補から1つを選ぶ。race_key(text)の直近開催条件が
    place/track_type/distance と一致するものがあればそれを優先。
    無ければ先頭 (発見順、= sponsors配列の順) を返す。"""
    if not candidates:
        return None, None
    if len(candidates) == 1 or (place is None and track_type is None and distance is None):
        return candidates[0]
    for text, how in candidates:
        cond = _condition_of(known_keys, race_key(text))
        if _matches_condition(cond, place, track_type, distance):
            return text, how
    return candidates[0]


def resolve_full_name(name, known_keys, *, place=None, track_type=None, distance=None,
                      sponsors=None, aliases=None):
    """レース名表記を既知キーに突合し、突合に使った「冠除去後のフルテキスト」を返す
    (4文字に切り詰める前の文字列。display_name生成用)。match_key() はこの結果に
    race_key() を適用したものを返す (aliasの場合は既にキーそのものなのでそのまま)。

    戻り値: (full_text, grade, how)。一致しなければ (None, None, None)。
    how は 'alias' | 'exact' | 'last_token' | 'strip_sponsor' | 'sponsor_list' のいずれか。
    """
    keys = _known_key_set(known_keys)
    base, grade = normalize_race_name(name)
    raw = str(name or "").strip()

    if aliases is None:
        aliases = load_aliases()
    # 別名表は最優先で参照する (手動メンテ用)。base / 元の表記いずれかで引ける。
    for candidate_name in (base, raw):
        alias_key = aliases.get(candidate_name)
        if alias_key is not None and alias_key in keys:
            return alias_key, grade, "alias"

    if not base:
        return None, None, None

    # 実装上の注記 (SPEC §2.1.3 の a〜d の優先順そのままだと、冠付きの長い正式名
    # (netkeiba由来) の先頭4文字が、数十年前に引退した無関係な重賞の短い略称
    # (例 "日刊スポＨG3"=1986〜1995年のみ存在) とたまたま一致してしまう事故が
    # 実データで確認された (日刊スポ賞中山金杯 / 日刊スポシンザン記念 が両方とも
    # 誤って同じ偶然一致キーに丸められる)。冠除去の方が明らかに確実な手掛かりで
    # あるため、b→c→d (冠除去系) を先に試し、どれも当たらない場合のみ最後に
    # a (素の全体一致) を試す。これにより「最初に一致したものを返す」という
    # 仕様の趣旨 (=より確実な根拠を優先する) を保ちつつ事故を避ける。

    # (b) 冠除去: 空白区切りの最後のトークン
    if " " in base:
        last_token = base.rsplit(" ", 1)[-1]
        if last_token and race_key(last_token) in keys:
            return last_token, grade, "last_token"

    # (c) 冠パターン除去: 先頭から (賞|杯|賞典) で終わる最短の冠を1回だけ除去
    m = _SPONSOR_PATTERN_RE.match(base)
    if m:
        remainder = base[m.end():]
        if len(remainder) >= 2 and race_key(remainder) in keys:
            return remainder, grade, "strip_sponsor"

    # (d) 冠リスト除去 (data/graded_sponsors.json)
    if sponsors is None:
        sponsors = load_sponsors()
    candidates = []
    for sponsor in sponsors:
        if sponsor and base.startswith(sponsor):
            remainder = base[len(sponsor):]
            if len(remainder) >= 2 and race_key(remainder) in keys:
                candidates.append((remainder, "sponsor_list"))
    if candidates:
        text, how = _pick_best(candidates, known_keys, place, track_type, distance)
        if text is not None:
            return text, grade, how

    # (a) base全体のキー (冠除去系がどれも当たらなかった場合の既定パス)
    if race_key(base) in keys:
        return base, grade, "exact"

    return None, None, None


def match_key(name, known_keys, *, place=None, track_type=None, distance=None,
             sponsors=None, aliases=None):
    """レース名表記を既知キーに突合する。

    戻り値: (key, grade, how)。一致しなければ (None, None, None)。
    how は 'alias' | 'exact' | 'last_token' | 'strip_sponsor' | 'sponsor_list' のいずれか。
    """
    full_text, grade, how = resolve_full_name(
        name, known_keys, place=place, track_type=track_type, distance=distance,
        sponsors=sponsors, aliases=aliases)
    if full_text is None:
        return None, None, None
    key = full_text if how == "alias" else race_key(full_text)
    return key, grade, how
