"""
スコアリングエンジン
====================
db-keiba.com 由来のコース別ファクター統計 (data_files/<venue>/factors/<芝|ダート><距離>.csv)
と馬情報を照合し、複勝率ベース+回収率補正の評価点(スコア)を算出する。

設定: data_files/common/score_weights.json (重み・パラメータはコード変更なしで調整可能)
"""
import csv
import json
import os
import re
import unicodedata

try:
    from analysis import VENUE_SLUG_MAP
except ImportError:
    VENUE_SLUG_MAP = {
        "中山": "nakayama", "阪神": "hanshin", "東京": "tokyo", "京都": "kyoto",
        "中京": "chukyo", "新潟": "niigata", "福島": "fukushima", "小倉": "kokura",
        "札幌": "sapporo", "函館": "hakodate", "共通": "common",
    }

FACTOR_LABELS = {
    "father_w": "種牡馬", "jockey_w": "騎手", "frame": "枠順",
    "averunningstyle": "脚質", "distance": "距離変更",
    "surface": "コース替", "stable_trainer": "所属",
}

DEFAULT_CFG = {
    "version": 0,
    "params": {
        "scale_k": 100, "clamp_min": -8.0, "clamp_max": 12.0,
        "shrinkage_n0": 20, "min_starts": 5,
        "roi_bonus": {"show_roi_threshold": 100, "win_roi_threshold": 100, "points": 1.5},
        "grade_bonus": {"◎": 8.0, "〇": 4.0, "△": 1.5},
        "buysell_points": {"買い": 3.0, "消し": -4.0},
        "buysell_clamp": 6.0,
        "ability": {
            "time_k": 2.0, "time_clamp": 8.0,
            "recency": [1.0, 0.85, 0.7, 0.55],
            "class_k": 0.12,
            "pos_factor": {"1": 1.0, "2": 0.7, "3": 0.5, "4": 0.25, "5": 0.25},
            "agari_best_ratio": 0.3, "agari_bonus": 1.5,
        },
    },
    "weights": {
        "father_w": 1.0, "jockey_w": 1.0, "frame": 1.0, "averunningstyle": 1.0,
        "distance": 0.8, "surface": 0.8, "stable_trainer": 0.5, "sire_buysell": 1.0,
        "ability": 1.0,
    },
    "style_map": {
        "◀◁◁◁": ["逃げ"], "◀◀◁◁": ["逃げ", "先行"], "◁◀◁◁": ["先行"],
        "◁◀◀◁": ["先行", "差し"], "◁◁◀◁": ["差し"], "◁◁◀◀": ["差し", "追込"],
        "◁◁◁◀": ["追込"],
    },
}

_NUM_KEYS = ("n1", "n2", "n3", "out", "starts")
_RATE_KEYS = ("win_rate", "quinella_rate", "show_rate", "win_roi", "show_roi")


# ─── 読み込み ────────────────────────────────────────────────────────────────

def load_score_weights(base_dir):
    """score_weights.json を読み込む。失敗時はデフォルト設定を返す。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_files", "common", "score_weights.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_CFG


def load_factor_table(venue, race_type, dist_val, base_dir):
    """
    factors CSV を読み込み {"baseline": row, "father_w": {正規化名: row}, ...} を返す。
    ファイルが無い会場・コースは None (スコア算出なし)。
    """
    slug = VENUE_SLUG_MAP.get(venue)
    if not slug:
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_files", slug, "factors", f"{race_type}{dist_val}.csv")
    if not os.path.exists(path):
        return None
    try:
        table = {}
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for raw in csv.DictReader(f):
                row = _parse_row(raw)
                ftype = raw.get("factor_type", "")
                if ftype == "baseline":
                    table["baseline"] = row
                elif ftype:
                    table.setdefault(ftype, {})[_norm(raw.get("entity", ""))] = row
        if "baseline" not in table or table["baseline"].get("show_rate") is None:
            return None
        return table
    except Exception:
        return None


def _parse_row(raw):
    row = {"entity": raw.get("entity", "")}
    for k in _NUM_KEYS:
        try:
            row[k] = int(float(raw[k]))
        except (KeyError, ValueError, TypeError):
            row[k] = 0
    for k in _RATE_KEYS:
        try:
            row[k] = float(raw[k])
        except (KeyError, ValueError, TypeError):
            row[k] = None
    return row


# ─── 血統辞典 買い/消しルール (競馬血統総辞典) ──────────────────────────────

_BUYSELL_CACHE = {"rules": None, "lineage": None}

# サンデー系大系統 / ディープ系・Tサンデー系 に対応する syuboba.csv の系統グループ
_BMS_GROUP_SETS = {
    "sunday_all": {"ディープインパクト系", "ステイゴールド系", "サンデーサイレンス系"},
    "deep_tsunday": {"ディープインパクト系", "サンデーサイレンス系"},
}


def load_buysell_rules(base_dir):
    """sire_buysell_rules.json を {正規化種牡馬名: [rule,...]} で返す (モジュールキャッシュ)。"""
    if _BUYSELL_CACHE["rules"] is not None:
        return _BUYSELL_CACHE["rules"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_files", "common", "sire_buysell_rules.json")
    index = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for rule in json.load(f).get("rules", []):
                index.setdefault(_norm(rule.get("sire", "")), []).append(rule)
    except Exception:
        pass
    _BUYSELL_CACHE["rules"] = index
    return index


def _load_bms_lineage():
    """syuboba.csv → {種牡馬名: 系統グループ名} (母父の系統判定用、モジュールキャッシュ)。"""
    if _BUYSELL_CACHE["lineage"] is not None:
        return _BUYSELL_CACHE["lineage"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_files", "common", "syuboba.csv")
    lineage = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            for line in f:
                parts = [p.strip() for p in line.split(",") if p.strip()]
                if not parts:
                    continue
                group = parts[0].replace("種牡馬", "")
                for sire in parts[1:]:
                    lineage[_norm(sire)] = group
                # 系統の始祖自身も同グループに (例: 母父ディープインパクト → ディープインパクト系)
                root = group[:-1] if group.endswith("系") else group
                lineage.setdefault(_norm(root), group)
    except Exception:
        pass
    _BUYSELL_CACHE["lineage"] = lineage
    return lineage


def straight_type(venue, race_type, dist):
    """コースの直線長短の近似判定。判定不能は None。"""
    if venue in ("東京", "中京"):
        return "long"
    if venue == "新潟":
        if race_type == "芝" and dist == 1000:
            return None  # 直線1000mは長短の意図と異なるため判定しない
        return "long" if (race_type == "芝" and dist >= 1600) else "short"
    if venue == "阪神":
        return "long" if (race_type == "芝" and dist in (1600, 1800, 2400, 2600)) else "short"
    if venue == "京都":
        return "long" if (race_type == "芝" and dist >= 1800) else "short"
    if venue in ("中山", "福島", "小倉", "札幌", "函館"):
        return "short"
    return None


def _hist_pos_ratios(h):
    """過去走の (1角位置/頭数) 比率リスト"""
    ratios = []
    for run in (h.get("hist") or []):
        if not run:
            continue
        corners = str(run.get("corners", "-"))
        total = str(run.get("total", "-"))
        first = corners.split("-")[0].strip()
        if first.isdigit() and total.isdigit() and int(total) > 0:
            ratios.append(int(first) / int(total))
    return ratios


def _hist_agari_ratios(h):
    ratios = []
    for run in (h.get("hist") or []):
        if not run:
            continue
        m = re.match(r"(\d+)/(\d+)", str(run.get("agari_rank", "")))
        if m and int(m.group(2)) > 0:
            ratios.append(int(m.group(1)) / int(m.group(2)))
    return ratios


def _class_cond_ok(cond, race_context):
    """クラス条件の判定。判定不能は None。"""
    rc = str(race_context.get("race_class", ""))
    age = race_context.get("age_cond", "")
    try:
        from analysis import get_class_rank
        rank = get_class_rank(rc)
    except Exception:
        rank = 0
    if cond == "shinba":
        return "新馬" in rc
    if cond == "kobakongo":
        return (age == "古馬混合") if age else None
    if cond == "nisai_sansai":
        return (age in ("2歳限定", "3歳限定")) if age else None
    if cond == "below_1win":
        return rank < 30 if rank else None
    if cond == "ge_2win":
        return rank >= 40 if rank else None
    if cond == "joken":
        return (20 <= rank <= 50) if rank else None
    return None


def _eval_buysell_segment(seg, h, race_context):
    """
    OR節1つを評価。
    戻り値: (matched, notes)  matched=None は評価可能述語なし。
    判定不能な述語は notes に落として無視 (要確認として表示)。
    """
    notes = list(seg.get("unknown_notes", []))
    checks = []

    venue = race_context.get("venue", "")
    race_type = race_context.get("type") or race_context.get("race_type") or ""
    dist = race_context.get("dist") or race_context.get("dist_val") or 0

    if seg.get("surface"):
        checks.append(seg["surface"] == race_type)
    if seg.get("venue"):
        checks.append(seg["venue"] == venue)
    if seg.get("dist_list") is not None:
        ok = dist in seg["dist_list"] or (seg.get("dist_min_alt") and dist >= seg["dist_min_alt"])
        checks.append(bool(ok))
    else:
        if seg.get("dist_min") is not None:
            checks.append(dist >= seg["dist_min"])
        if seg.get("dist_max") is not None:
            checks.append(dist <= seg["dist_max"])
    if seg.get("straight"):
        st = straight_type(venue, race_type, dist)
        if st is None:
            notes.append("直線長短判定不能")
        else:
            checks.append(st == seg["straight"])
    if seg.get("sex"):
        sex = str(h.get("sex_age", ""))[:1]
        checks.append(sex in seg["sex"])
    if seg.get("waku_min") is not None:
        w = h.get("w_num")
        checks.append(w is not None and w >= seg["waku_min"])
    if seg.get("waku_max") is not None:
        w = h.get("w_num")
        checks.append(w is not None and w <= seg["waku_max"])
    if seg.get("prev_weight_min") is not None or seg.get("prev_weight_max") is not None:
        hist = h.get("hist") or []
        m = re.search(r"(\d{3})", str(hist[0].get("weight", "")) if hist and hist[0] else "")
        if m:
            wt = int(m.group(1))
            if seg.get("prev_weight_min") is not None:
                checks.append(wt >= seg["prev_weight_min"])
            if seg.get("prev_weight_max") is not None:
                checks.append(wt <= seg["prev_weight_max"])
        else:
            notes.append("前走馬体重不明")
    if seg.get("dist_extend"):
        checks.append(h.get("dist_diff") == "延長")
    if seg.get("nigekiri_prev"):
        hist = h.get("hist") or []
        ok = False
        if hist and hist[0]:
            first = str(hist[0].get("corners", "-")).split("-")[0].strip()
            rank = str(hist[0].get("rank", ""))
            ok = first == "1" and rank.isdigit() and int(rank) <= 3
        checks.append(ok)
    if seg.get("senko_recent") is not None:
        ratios = _hist_pos_ratios(h)
        if ratios:
            checks.append(any(r <= seg["senko_recent"] for r in ratios))
            notes.append("≈テンパターンは通過順で近似")
        else:
            notes.append("近走位置取り不明")
    if seg.get("senko_never") is not None:
        ratios = _hist_pos_ratios(h)
        if ratios:
            checks.append(all(r > seg["senko_never"] for r in ratios))
            notes.append("≈テンパターンは通過順で近似")
        else:
            notes.append("近走位置取り不明")
    if seg.get("agari_recent") is not None:
        ratios = _hist_agari_ratios(h)
        if ratios:
            checks.append(any(r <= seg["agari_recent"] for r in ratios))
            notes.append("≈上がりパターンは上がり順位で近似")
        else:
            notes.append("近走上がり不明")
    if seg.get("class_cond"):
        ok = _class_cond_ok(seg["class_cond"], race_context)
        if ok is None:
            notes.append("クラス条件判定不能")
        else:
            checks.append(ok)
    if seg.get("bms_group"):
        grp_def = seg["bms_group"]
        target_groups = _BMS_GROUP_SETS.get(grp_def.get("group"), set())
        bms_lineage = _load_bms_lineage().get(_norm(h.get("bms", "")))
        if bms_lineage is None:
            notes.append(f"母父系統未登録({h.get('bms', '-')})")
        else:
            in_group = bms_lineage in target_groups
            checks.append(not in_group if grp_def.get("negate") else in_group)

    if not checks:
        return None, notes
    return all(checks), notes


def eval_sire_buysell(h, race_context, cfg):
    """
    馬の父に対する買い/消しルールを評価。
    戻り値: (points, detail_lines)
    """
    weight = cfg.get("weights", {}).get("sire_buysell")
    if not weight:
        return 0.0, []
    rules_index = load_buysell_rules(None)
    rules = None
    sire_norm = _norm(h.get("sire", ""))
    if sire_norm and sire_norm in rules_index:
        rules = rules_index[sire_norm]
    if not rules:
        return 0.0, []

    params = cfg.get("params", DEFAULT_CFG["params"])
    pts_map = params.get("buysell_points", {"買い": 3.0, "消し": -4.0})
    clamp = params.get("buysell_clamp", 6.0)

    total = 0.0
    details = []
    for rule in rules:
        matched_notes = None
        for seg in rule.get("segments", []):
            m, notes = _eval_buysell_segment(seg, h, race_context)
            if m:
                matched_notes = notes
                break
        if matched_notes is None:
            continue
        pts = pts_map.get(rule["kubun"], 0.0) * weight
        total += pts
        text = rule["text"]
        if len(text) > 42:
            text = text[:42] + "…"
        note_str = ""
        uniq_notes = sorted(set(matched_notes))
        if uniq_notes:
            note_str = f" (要確認: {'、'.join(uniq_notes[:3])})"
        details.append(f"血統辞典{rule['kubun']}: {text} → {pts:+.1f}{note_str}")

    total = max(-clamp, min(clamp, total))
    return total, details


# ─── 能力サブスコア (タイム指数 / クラス実績 / 上がり質) ────────────────────

_STD_TIMES_CACHE = {"data": None}


def _load_standard_times():
    """venue_standard_times.json (場×芝ダ×距離×クラス×馬場の基準タイム)"""
    if _STD_TIMES_CACHE["data"] is not None:
        return _STD_TIMES_CACHE["data"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_files", "common", "venue_standard_times.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            _STD_TIMES_CACHE["data"] = json.load(f).get("times", {})
    except Exception:
        _STD_TIMES_CACHE["data"] = {}
    return _STD_TIMES_CACHE["data"]


def class_label(race_name):
    """レース名 → クラスラベル (build_ability_db.py と同一分類)"""
    s = str(race_name).upper()
    if any(g in s for g in ("G1", "G2", "G3", "GⅠ", "GⅡ", "GⅢ", "JG")):
        return "重賞"
    if "(L)" in s or "オープ" in s or "OP" in s:
        return "オープン"
    if "3勝" in s or "３勝" in s or "1600万" in s:
        return "3勝クラス"
    if "2勝" in s or "２勝" in s or "1000万" in s:
        return "2勝クラス"
    if "1勝" in s or "１勝" in s or "500万" in s or "５００万" in s:
        return "1勝クラス"
    if "未勝利" in s:
        return "未勝利"
    if "新馬" in s:
        return "新馬"
    return "オープン"


def std_time(place, track, dist, rclass, cond):
    """基準タイム取得。馬場状態は 指定→良→登録済み中央値 の順でフォールバック。"""
    node = _load_standard_times().get(place, {}).get(track, {}).get(str(dist), {}).get(rclass)
    if not node:
        return None
    if cond in node:
        return node[cond]
    if "良" in node:
        return node["良"]
    vals = sorted(node.values())
    return vals[len(vals) // 2] if vals else None


def parse_run_time(t):
    """'1:33.6' / '2.01.5' / '59.4' → 秒 (数値ならそのまま秒として扱う)"""
    if isinstance(t, (int, float)):
        return float(t)
    s = str(t).strip()
    m = re.match(r"^(?:(\d)[:\.])?(\d{1,2})[:\.](\d)$|^(\d{2})\.(\d)$", s)
    if not m:
        m2 = re.match(r"^(\d):(\d{2})\.(\d)$", s)
        if m2:
            return int(m2.group(1)) * 60 + int(m2.group(2)) + int(m2.group(3)) / 10.0
        return None
    if m.group(4):  # 59.4 形式
        return int(m.group(4)) + int(m.group(5)) / 10.0
    minute = int(m.group(1)) if m.group(1) else 0
    return minute * 60 + int(m.group(2)) + int(m.group(3)) / 10.0


def _run_course_info(run):
    """過去走1件から (場, 芝ダ, 距離, クラス, 馬場) を抽出"""
    place = run.get("place")
    course = str(run.get("course", ""))
    if not place or "障" in course:
        return None
    m = re.search(r"(\d{3,4})", course)
    if not m:
        return None
    dist = int(m.group(1))
    track = "芝" if "芝" in course else ("ダート" if "ダ" in course else None)
    if track is None:
        return None
    cond = ""
    for ch in str(run.get("condition", "")):
        if ch in "良稍重不":
            cond = ch
            break
    cond_full = {"良": "良", "稍": "稍", "重": "重", "不": "不"}.get(cond, "")
    return place, track, dist, class_label(run.get("race_name", "")), cond_full


def eval_ability(h, cfg):
    """
    馬自身の能力サブスコア。
    戻り値: (points, detail_lines)
      - タイム指数: 過去走タイム vs 場×距離×クラス×馬場の基準タイム (最良走を採用)
      - クラス実績: 通用した最高クラス×着順 (直近ほど重い)
      - 上がり質: 近走で上がり上位経験
    """
    params = cfg.get("params", DEFAULT_CFG["params"]).get(
        "ability", DEFAULT_CFG["params"]["ability"])
    weight = cfg.get("weights", {}).get("ability")
    if not weight:
        return 0.0, []

    recency = params.get("recency", [1.0, 0.85, 0.7, 0.55])
    total = 0.0
    details = []
    hist = [r for r in (h.get("hist") or []) if r]

    # ── タイム指数 (利用可能な過去走の最良値) ──
    best_time = None  # (pts, label)
    for i, run in enumerate(hist[:4]):
        tsec = parse_run_time(run.get("run_time", "-"))
        info = _run_course_info(run)
        if tsec is None or info is None:
            continue
        place, track, dist, rclass, cond = info
        base = std_time(place, track, dist, rclass, cond)
        if base is None:
            continue
        diff = base - tsec  # 正 = 基準勝ちタイムより速い
        rw = recency[i] if i < len(recency) else recency[-1]
        pts = max(-params.get("time_clamp", 8.0),
                  min(params.get("time_clamp", 8.0), diff * params.get("time_k", 2.0))) * rw
        label = (f"{['前走','2走前','3走前','4走前'][i]} {place}{track}{dist}"
                 f"({rclass}{cond or ''}) 基準{-diff:+.1f}秒")
        if best_time is None or pts > best_time[0]:
            best_time = (pts, label)
    if best_time is not None:
        pts = best_time[0] * weight
        total += pts
        details.append(f"能力タイム: {best_time[1]} → {pts:+.1f}")

    # ── クラス実績 (最高評価の1走) ──
    try:
        from analysis import get_class_rank
    except ImportError:
        get_class_rank = lambda s: 0  # noqa: E731
    pos_factor = params.get("pos_factor", {"1": 1.0, "2": 0.7, "3": 0.5, "4": 0.25, "5": 0.25})
    best_class = None
    for i, run in enumerate(hist[:4]):
        rank_s = str(run.get("rank", ""))
        if not rank_s.isdigit():
            continue
        pf = pos_factor.get(rank_s, 0.0)
        if not pf:
            continue
        crank = get_class_rank(str(run.get("race_name", "")))
        if not crank:
            crank = {"重賞": 70, "オープン": 60, "3勝クラス": 50, "2勝クラス": 40,
                     "1勝クラス": 30, "未勝利": 20, "新馬": 10}.get(
                         class_label(run.get("race_name", "")), 0)
        rw = recency[i] if i < len(recency) else recency[-1]
        pts = crank * params.get("class_k", 0.12) * pf * rw
        label = f"{class_label(run.get('race_name', ''))}{rank_s}着"
        if best_class is None or pts > best_class[0]:
            best_class = (pts, label)
    if best_class is not None and best_class[0] > 0:
        pts = best_class[0] * weight
        total += pts
        details.append(f"能力クラス: {best_class[1]} → {pts:+.1f}")

    # ── 上がり質 ──
    ratios = _hist_agari_ratios(h)
    if ratios and min(ratios) <= params.get("agari_best_ratio", 0.3):
        pts = params.get("agari_bonus", 1.5) * weight
        total += pts
        details.append(f"能力上がり: 近走上がり上位{int(min(ratios) * 100)}% → {pts:+.1f}")

    return total, details


# ─── 照合ユーティリティ ──────────────────────────────────────────────────────

def _norm(s):
    """NFKC正規化 + 空白/ドット/減量記号を除去 (JRA側は数字・ドット除去済のため合わせる)"""
    s = unicodedata.normalize("NFKC", str(s))
    return re.sub(r"[\s\.・．☆★▲△◇]", "", s).strip()


def _match_entity(entity_map, raw_name):
    """正規化完全一致 → 3文字以上かつ一意なら部分一致fallback。不一致は None。"""
    if not raw_name or raw_name == "-" or not entity_map:
        return None
    name = _norm(raw_name)
    if not name:
        return None
    if name in entity_map:
        return entity_map[name]
    if len(name) >= 3:
        hits = [row for ent, row in entity_map.items()
                if len(ent) >= 3 and (name in ent or ent in name)]
        if len(hits) == 1:
            return hits[0]
    return None


def _prev_course_info(h):
    """前走の (距離, 芝orダ) を hist[0] から抽出。障害・不明は None。"""
    hist = h.get("hist") or []
    if not hist or not hist[0]:
        return None, None
    course = str(hist[0].get("course", ""))
    if "障" in course:
        return None, None
    m = re.search(r"(\d{3,4})", course)
    dist = int(m.group(1)) if m else None
    surf = "芝" if "芝" in course else ("ダ" if "ダ" in course else None)
    return dist, surf


def _distance_label(dist_val, prev_dist):
    if prev_dist is None:
        return None
    diff = dist_val - prev_dist
    if diff == 0:
        return "距離変更なし"
    ad = abs(diff)
    if ad >= 500:
        band = "500m以上"
    elif ad >= 250:
        band = "300m-400m"
    else:
        band = "100m-200m"
    return band + ("延長" if diff > 0 else "短縮")


# ─── スコア計算 ──────────────────────────────────────────────────────────────

def _factor_points(row, baseline_show, params, weight):
    """1ファクター分の得点。データ不足時は None。"""
    starts = row.get("starts") or 0
    show = row.get("show_rate")
    if show is None or starts < params.get("min_starts", 5):
        return None, ""
    dev = (show - baseline_show) / 100.0
    shrunk = dev * starts / (starts + params.get("shrinkage_n0", 20))
    pts = params.get("scale_k", 100) * shrunk
    pts = max(params.get("clamp_min", -8.0), min(params.get("clamp_max", 12.0), pts))
    pts *= weight
    roi_note = ""
    rb = params.get("roi_bonus", {})
    win_roi, show_roi = row.get("win_roi"), row.get("show_roi")
    if ((show_roi is not None and show_roi >= rb.get("show_roi_threshold", 100)) or
            (win_roi is not None and win_roi >= rb.get("win_roi_threshold", 100))):
        bonus = rb.get("points", 1.5) * weight
        pts += bonus
        roi_note = f" (回収率≧100 +{bonus:.1f})"
    return pts, roi_note


def compute_score(h, race_context, factor_table, cfg):
    """
    馬1頭のスコアを算出。
    戻り値: (score | None, score_details[])
    factor_table が None、または例外発生時は (None, [])。
    """
    if factor_table is None:
        return None, []
    try:
        return _compute_score_inner(h, race_context, factor_table, cfg)
    except Exception:
        return None, []


def _compute_score_inner(h, race_context, factor_table, cfg):
    params = cfg.get("params", DEFAULT_CFG["params"])
    weights = cfg.get("weights", DEFAULT_CFG["weights"])
    style_map = cfg.get("style_map", DEFAULT_CFG["style_map"])
    baseline_show = factor_table["baseline"]["show_rate"]

    dist_val = race_context.get("dist") or race_context.get("dist_val") or 0
    race_type = race_context.get("type") or race_context.get("race_type") or ""
    prev_dist, prev_surf = _prev_course_info(h)

    total = 0.0
    details = []

    def add(factor, entity_label, row):
        w = weights.get(factor)
        if not w or row is None:  # 重み0のファクターは算入も表示もしない
            return
        pts, roi_note = _factor_points(row, baseline_show, params, w)
        if pts is None:
            return
        nonlocal total
        total += pts
        details.append(
            f"{FACTOR_LABELS.get(factor, factor)} {entity_label}: "
            f"複{row['show_rate']:.1f}% (基準{baseline_show:.1f}%) n={row['starts']}"
            f" → {pts:+.1f}{roi_note}"
        )

    # 種牡馬
    add("father_w", h.get("sire", "-"),
        _match_entity(factor_table.get("father_w"), h.get("sire")))

    # 騎手
    add("jockey_w", h.get("jock", "-"),
        _match_entity(factor_table.get("jockey_w"), h.get("jock")))

    # 枠順
    w_num = h.get("w_num")
    if w_num:
        fmap = factor_table.get("frame") or {}
        row = fmap.get(_norm(f"{w_num}枠")) or fmap.get(_norm(str(w_num)))
        add("frame", f"{w_num}枠", row)

    # 脚質 (複数該当は平均、重み0なら算入しない)
    styles = style_map.get(h.get("kyakushitsu", ""), []) if weights.get("averunningstyle") else []
    if styles:
        smap = factor_table.get("averunningstyle") or {}
        rows = [smap.get(_norm(s)) for s in styles]
        rows = [r for r in rows if r]
        if rows:
            w = weights.get("averunningstyle", 0)
            pts_list = []
            for r in rows:
                p, _ = _factor_points(r, baseline_show, params, w)
                if p is not None:
                    pts_list.append((p, r))
            if pts_list:
                avg = sum(p for p, _ in pts_list) / len(pts_list)
                total += avg
                label = "/".join(styles)
                shows = "/".join(f"{r['show_rate']:.1f}" for _, r in pts_list)
                details.append(
                    f"{FACTOR_LABELS['averunningstyle']} {label}: 複{shows}% "
                    f"(基準{baseline_show:.1f}%) → {avg:+.1f}"
                )

    # 距離変更 (前走距離との実差分 → db-keibaの7段階ラベル)
    d_label = _distance_label(dist_val, prev_dist)
    if d_label:
        dmap = factor_table.get("distance") or {}
        add("distance", d_label, dmap.get(_norm(d_label)))

    # コース替わり (前走芝/ダ → 今回芝/ダ)
    if prev_surf:
        cur = "芝" if race_type == "芝" else "ダ"
        s_label = f"{prev_surf}→{cur}"
        smap = factor_table.get("surface") or {}
        add("surface", s_label, smap.get(_norm(s_label)))

    # 所属 (美浦/栗東)
    affi = h.get("affi")
    if affi in ("美浦", "栗東"):
        tmap = factor_table.get("stable_trainer") or {}
        add("stable_trainer", affi, tmap.get(_norm(affi)))

    # 血統辞典 買い/消し (競馬血統総辞典)
    bs_pts, bs_details = eval_sire_buysell(h, race_context, cfg)
    if bs_details:
        total += bs_pts
        details.extend(bs_details)

    # 能力サブスコア (タイム指数/クラス実績/上がり質)
    ab_pts, ab_details = eval_ability(h, cfg)
    if ab_details:
        total += ab_pts
        details.extend(ab_details)

    # 既存◎〇△判定ボーナス
    gb = params.get("grade_bonus", {})
    grade = h.get("grade", "")
    if grade in gb and gb[grade]:
        total += gb[grade]
        details.append(f"判定{grade} → {gb[grade]:+.1f}")

    score = round(total, 1)
    details.append(f"合計: {score:+.1f}")
    return score, details
