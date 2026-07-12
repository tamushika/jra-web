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

def load_score_weights(base_dir, filename="score_weights.json"):
    """重み設定JSONを読み込む (filename指定でWIN5用等に切替)。失敗時はデフォルト設定。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_files", "common", filename)
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
        if run.get("agari_ratio") is not None:
            try:
                ratios.append(float(run["agari_ratio"]))
                continue
            except (TypeError, ValueError):
                pass
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


# ─── 市場 (単勝オッズ) 統合 ──────────────────────────────────────────────────

def eval_market(h, cfg):
    """
    単勝オッズを市場の勝率予測としてスコアに統合する。
    市場点 = odds_k × ln(odds_ref / オッズ)。odds_ref はレース内順位に影響しない
    表示用の基準 (デフォルト10倍 = ±0の境界)。
    バックテスト根拠: 市場(人気順)の勝ち馬カバレッジはモデル単体より高く
    (k=1: 31.0% vs 20.8%)、ブレンドで的中率が向上する。
    """
    params = cfg.get("params", {}).get("market", {})
    odds_k = params.get("odds_k", 0.0)
    if not odds_k:
        return 0.0, []
    try:
        odds = float(h.get("odds", 0))
    except (TypeError, ValueError):
        return 0.0, []
    if odds <= 1.0 or odds >= 999:  # 未発売・取得失敗
        return 0.0, []
    import math
    ref = params.get("odds_ref", 10.0)
    clamp = params.get("clamp", 10.0)
    pts = max(-clamp, min(clamp, odds_k * math.log(ref / odds)))
    return pts, [f"市場: 単勝{odds}倍 → {pts:+.1f}"]


# ─── 道悪適性 (当日馬場が稍/重/不のときのみ発動) ─────────────────────────────

def eval_wet_aptitude(h, race_context, cfg):
    """
    相対道悪適性: 直近4走に道悪(稍/重/不)と良馬場の両方の経験がある馬について、
    ベスト着順を比較し「道悪の方が走る馬」を加点 /「道悪の方が悪い馬」を減点。
    当日馬場が道悪のときのみ発動。
    バックテスト根拠 (backtest_wet.py, 2024-2025): 道悪の方が良い馬は道悪日の
    単回収94.4% (良馬場日65.9%) と市場が過小評価。単純な道悪好走フラグは
    良馬場でも同成績 (能力の代理) のため不採用。
    """
    params = cfg.get("params", {}).get("wet_aptitude", {})
    if not params:
        return 0.0, []
    cond = str(race_context.get("baba_cond", ""))[:1]
    if cond not in ("稍", "重", "不"):
        return 0.0, []

    wet_ranks, dry_ranks = [], []
    for run in (h.get("hist") or [])[:4]:
        if not run:
            continue
        c = ""
        for ch in str(run.get("condition", "")):
            if ch in "良稍重不":
                c = ch
                break
        rank_s = str(run.get("rank", ""))
        if not c or not rank_s.isdigit():
            continue
        (dry_ranks if c == "良" else wet_ranks).append(int(rank_s))
    if not wet_ranks or not dry_ranks:
        return 0.0, []  # 相対比較できない馬は増減なし

    gap = min(wet_ranks) - min(dry_ranks)  # 負 = 道悪の方が着順が良い
    threshold = params.get("rank_gap", 2)
    if gap <= -threshold:
        pts = params.get("bonus_better", 2.0)
        label = "道悪の方が良い"
    elif gap >= threshold:
        pts = params.get("penalty_worse", -1.5)
        label = "道悪の方が悪い"
    else:
        return 0.0, []
    detail = (f"道悪適性: 道悪ベスト{min(wet_ranks)}着 vs 良ベスト{min(dry_ranks)}着 "
              f"({label}) 当日{cond} → {pts:+.1f}")
    return pts, [detail]


# ─── MLスコア (ロジスティック回帰, WIN5用) ───────────────────────────────────

_ML_CACHE = {"model": None}

PCI_TRACK_MEAN = {"芝": 51.96, "ダート": 45.83}

_ML_LABELS = {
    "j_pts": "騎手", "f_pts": "枠順", "tfeat": "タイム", "cfeat": "クラス実績",
    "agari_flag": "上がり", "ln_odds": "市場", "prev_rank": "前走着順",
    "ln_interval": "間隔", "age": "年齢", "is_male": "性別", "kinryo": "斤量",
    "weight": "馬体重", "n_prior": "キャリア", "wet_match": "道悪適性",
    "dist_pts": "距離変更", "surf_pts": "コース替わり", "affi_pts": "所属",
    "pace_fit": "ペース適性", "course_fit": "同コース実績", "grade_pts": "好走条件",
    "sire_pts": "種牡馬",
}


def compute_pci(time_sec, agari_3f, distance):
    """PCIをJRA表示の走破時計・上がり3Fから計算する。

    ability.db作成時 (`extend_ability_from_neon.compute_pci`) と同一定義。
    """
    try:
        total = float(time_sec)
        agari = float(agari_3f)
        dist = int(distance)
    except (TypeError, ValueError):
        return None
    if total <= 0 or agari <= 0 or dist <= 600:
        return None
    try:
        return round(100.0 * ((total - agari) / (dist - 600)) / (agari / 600.0) - 50.0, 1)
    except ZeroDivisionError:
        return None


def _live_pci_dev(run):
    info = _run_course_info(run)
    if info is None:
        return None
    track = info[1]
    pci = run.get("pci")
    if pci is None:
        time_sec = run.get("time_sec")
        if time_sec is None:
            time_sec = parse_run_time(run.get("run_time", "-"))
        pci = compute_pci(time_sec, run.get("agari_3f"), info[2])
    try:
        return float(pci) - PCI_TRACK_MEAN[track]
    except (KeyError, TypeError, ValueError):
        return None


def attach_live_pace_features(horses):
    """レース全馬の直近4走PCIから学習時と同一定義のpace_fitを付与する。"""
    styles = []
    for horse in horses or []:
        devs = [_live_pci_dev(run) for run in (horse.get("hist") or [])[:4] if run]
        devs = [value for value in devs if value is not None]
        style = sum(devs) / len(devs) if devs else None
        horse["_pace_style"] = style
        if style is not None:
            styles.append(style)
    predicted = sum(styles) / len(styles) if len(styles) >= 5 else None
    for horse in horses or []:
        style = horse.get("_pace_style")
        horse["_predicted_pace"] = predicted
        horse["_pace_fit"] = style * predicted if style is not None and predicted is not None else 0.0
        horse["pace_fit_source"] = "jra_history" if style is not None and predicted is not None else "fallback_zero"
    return predicted


def load_ml_model():
    """win5_ml_model.json (backtest_ml.py --write が出力)。無ければ None。"""
    if _ML_CACHE["model"] is not None:
        return _ML_CACHE["model"] or None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_files", "common", "win5_ml_model.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            _ML_CACHE["model"] = json.load(f)
    except Exception:
        _ML_CACHE["model"] = {}
    return _ML_CACHE["model"] or None


_TRAINABLE_RULE_IDS_CACHE = {}


def _trainable_rule_ids(venue):
    """好走条件のうち、学習側 (backtest_ml.build_dataset →
    backtest_criteria.load_filtered_criteria) が「血統以外ルール」として採用する
    項番の集合。venue単位でキャッシュ (criteria.csv は実行中不変)。"""
    if venue in _TRAINABLE_RULE_IDS_CACHE:
        return _TRAINABLE_RULE_IDS_CACHE[venue]
    ids = set()
    try:
        import analysis
        base_dir = os.path.dirname(os.path.abspath(__file__))
        for rule in analysis.load_csv_criteria(venue, base_dir):
            if not analysis.is_pedigree_rule(rule):
                ids.add(rule["id"])
    except Exception:
        ids = set()
    _TRAINABLE_RULE_IDS_CACHE[venue] = ids
    return ids


def _training_style_grade(matches, venue):
    """h['ultra_matches'] (血統込み判定の該当ルール一覧) から、血統ルールの該当を
    除いた「学習と同一定義」の最良グレードを再集計する (analysis.evaluate_ultra の
    best_grade 更新ロジックと同一)。該当ルールは既に判定済みなので check_condition の
    再評価は不要 — フィルタと集計のみで学習側と同じ結果になる。"""
    keep_ids = _trainable_rule_ids(venue)
    best = ""
    for m in matches:
        if m.get("id") not in keep_ids:
            continue
        grade = m.get("grade", "")
        if grade == "◎" or (grade == "〇" and best != "◎") or (grade == "△" and not best):
            best = grade
    return best


def _ml_features(h, race_context, factor_table, cfg):
    """ライブデータから backtest_ml.build_dataset と同一定義の特徴量を構築"""
    import math
    params = cfg.get("params", DEFAULT_CFG["params"])
    weights = cfg.get("weights", DEFAULT_CFG["weights"])
    ab = params.get("ability", DEFAULT_CFG["params"]["ability"])
    recency = ab.get("recency", [1.0, 0.85, 0.7, 0.55])
    f = {k: 0.0 for k in _ML_LABELS}

    # 前走のコース情報 (距離変更/コース替わり/同コース実績で使用)
    hist_all = [r for r in (h.get("hist") or []) if r]
    prev_info = _run_course_info(hist_all[0]) if hist_all else None  # (場,芝ダ,距離,クラス,馬場)
    cur_type = race_context.get("race_type") or race_context.get("type")
    cur_dist = race_context.get("distance") or race_context.get("dist")

    # バイアス (騎手/枠/距離変更/コース替わり/所属, 勝率ベースの点数そのもの)
    if factor_table is not None:
        baseline = factor_table["baseline"].get("win_rate")
        if baseline is not None:
            def _bias(fmap, key):
                row = (fmap or {}).get(key) if key else None
                if row is None:
                    return None
                p, _ = _factor_points(row, baseline, params, 1.0, "win_rate")
                return p

            row = _match_entity(factor_table.get("jockey_w"), h.get("jock"))
            if row is not None:
                p, _ = _factor_points(row, baseline, params, weights.get("jockey_w", 0), "win_rate")
                if p is not None:
                    f["j_pts"] = p
            w_num = h.get("w_num")
            if w_num:
                row = (factor_table.get("frame") or {}).get(_norm(f"{w_num}枠"))
                if row is not None:
                    p, _ = _factor_points(row, baseline, params, weights.get("frame", 0), "win_rate")
                    if p is not None:
                        f["f_pts"] = p
            if prev_info and cur_dist:
                d_label = _distance_label(int(cur_dist), prev_info[2])
                p = _bias(factor_table.get("distance"), _norm(d_label) if d_label else None)
                if p is not None:
                    f["dist_pts"] = p
            if prev_info and cur_type:
                s_label = (f"{'芝' if prev_info[1] == '芝' else 'ダ'}"
                           f"→{'芝' if cur_type == '芝' else 'ダ'}")
                p = _bias(factor_table.get("surface"), _norm(s_label))
                if p is not None:
                    f["surf_pts"] = p
            affi = h.get("affi")
            if affi in ("美浦", "栗東"):
                p = _bias(factor_table.get("stable_trainer"), _norm(affi))
                if p is not None:
                    f["affi_pts"] = p
            # 種牡馬 (モデルが sire_pts を持つ場合のみ寄与。カードに父が載るためライブは常時計算可)
            sire = h.get("sire")
            if sire and sire != "-":
                row = _match_entity(factor_table.get("father_w"), sire)
                if row is not None:
                    p, _ = _factor_points(row, baseline, params, 1.0, "win_rate")
                    if p is not None:
                        f["sire_pts"] = p

    # 能力系 (backtestと同じ raw特徴量: clip(基準差, ±4秒)×recency の最良値)
    hist = [r for r in (h.get("hist") or []) if r]
    use_variant = ab.get("track_variant", False)
    tbest, cbest, amin = None, None, None
    for i, run in enumerate(hist[:4]):
        rw = recency[i] if i < len(recency) else recency[-1]
        tsec = run.get("time_sec")
        if tsec is None:
            tsec = parse_run_time(run.get("run_time", "-"))
        info = _run_course_info(run)
        if tsec is not None and info is not None:
            place, track, dist, rclass, cond = info
            base = std_time(place, track, dist, rclass, cond)
            if base is not None:
                variant = 0.0
                if use_variant:
                    dm = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", str(run.get("raw", "")))
                    if dm:
                        variant = track_variant(
                            f"{dm.group(1)}{int(dm.group(2)):02d}{int(dm.group(3)):02d}",
                            place, track)
                diff = max(-4.0, min(4.0, base + variant - tsec))
                v = diff * rw
                tbest = v if tbest is None else max(tbest, v)
        rank_s = str(run.get("rank", ""))
        if rank_s.isdigit():
            pf = {"1": 1.0, "2": 0.4, "3": 0.2}
            pf = {int(k): v for k, v in ab.get("pos_factor", pf).items()}
            p = pf.get(int(rank_s), 0.0)
            if p:
                crank = {"重賞": 70, "オープン": 60, "3勝クラス": 50, "2勝クラス": 40,
                         "1勝クラス": 30, "未勝利": 20, "新馬": 10}.get(
                             class_label(run.get("race_name", "")), 0)
                v = crank * p * rw
                cbest = v if cbest is None else max(cbest, v)
    ratios = _hist_agari_ratios(h)
    if ratios:
        amin = min(ratios)
    f["tfeat"] = tbest or 0.0
    f["cfeat"] = cbest or 0.0
    f["agari_flag"] = 1.0 if (amin is not None and amin <= ab.get("agari_best_ratio", 0.3)) else 0.0

    # 市場
    try:
        odds = float(h.get("odds", 0))
    except (TypeError, ValueError):
        odds = 0
    f["ln_odds"] = math.log(odds) if 1.0 < odds < 999 else math.log(30.0)

    # 追加素性 (欠損時の埋め値は backtest_ml と同一)
    prev_rank = 10
    if hist and str(hist[0].get("rank", "")).isdigit():
        prev_rank = min(int(hist[0]["rank"]), 18)
    f["prev_rank"] = prev_rank
    try:
        interval_days = int(h.get("interval_days"))
    except (TypeError, ValueError):
        interval_days = None
    if interval_days is not None:
        f["ln_interval"] = math.log(max(interval_days, 1))
    else:
        iv = str(h.get("iv", "-"))
        if "連闘" in iv:
            f["ln_interval"] = math.log(7)
        else:
            m = re.search(r"中(\d+)週", iv)
            f["ln_interval"] = math.log((int(m.group(1)) + 1) * 7) if m else math.log(30)
    age_m = re.search(r"(\d+)", str(h.get("sex_age", "")))
    f["age"] = int(age_m.group(1)) if age_m else 4
    f["is_male"] = 1.0 if str(h.get("sex_age", ""))[:1] in ("牡", "セ") else 0.0
    kg_m = re.search(r"(\d+(?:\.\d+)?)", str(h.get("kg", "")))
    f["kinryo"] = float(kg_m.group(1)) if kg_m else 55.0
    wt = None
    try:
        current_weight = int(h.get("current_weight"))
        if 300 <= current_weight <= 700:
            wt = current_weight
    except (TypeError, ValueError):
        pass
    if wt is not None:
        h["weight_source"] = h.get("weight_source") or "jra_live"
    elif hist:
        wt_m = re.search(r"(\d{3})", str(hist[0].get("weight", "")))
        if wt_m:
            wt = int(wt_m.group(1))
            h["weight_source"] = "previous_run"
    if wt is None:
        wt = 470
        h["weight_source"] = "default_470"
    f["weight"] = wt
    f["n_prior"] = len(hist[:4])

    # 道悪適性 (eval_wet_aptitude と同じ定義を ±1/0 のフラグに)
    cond = str(race_context.get("baba_cond", ""))[:1]
    if cond in ("稍", "重", "不"):
        wet_r, dry_r = [], []
        for run in hist[:4]:
            c = next((ch for ch in str(run.get("condition", "")) if ch in "良稍重不"), "")
            rank_s = str(run.get("rank", ""))
            if c and rank_s.isdigit():
                (dry_r if c == "良" else wet_r).append(int(rank_s))
        if wet_r and dry_r:
            gap = min(wet_r) - min(dry_r)
            if gap <= -2:
                f["wet_match"] = 1.0
            elif gap >= 2:
                f["wet_match"] = -1.0

    # 同コース実績: 直近4走に同場・同芝ダ・同距離で3着以内
    cur_venue = race_context.get("venue")
    if cur_venue and cur_type and cur_dist:
        for run in hist[:4]:
            info = _run_course_info(run)
            rank_s = str(run.get("rank", ""))
            if (info and rank_s.isdigit() and int(rank_s) <= 3
                    and info[0] == cur_venue and info[1] == cur_type
                    and info[2] == int(cur_dist)):
                f["course_fit"] = 1.0
                break

    # 好走条件判定 (T8: 学習側 grade_pts は血統以外ルールのみで定義されているため、
    # ML特徴量もそれに合わせる。画面表示用の h["grade"] は血統込みのまま変更しない)
    matches = h.get("ultra_matches")
    if matches is not None:
        grade_for_ml = _training_style_grade(matches, race_context.get("venue", ""))
    else:
        # ultra_matches 未対応の呼び出し元 (単体テスト等) 向けフォールバック
        grade_for_ml = h.get("grade", "")
    f["grade_pts"] = {"◎": 3.0, "〇": 2.0, "△": 1.0}.get(grade_for_ml, 0.0)

    # レース単位で attach_live_pace_features() が付与。欠損時のみ従来どおり0。
    try:
        f["pace_fit"] = float(h.get("_pace_fit", 0.0))
    except (TypeError, ValueError):
        f["pace_fit"] = 0.0
    return f


def compute_score_ml(h, race_context, factor_table, cfg):
    """
    MLスコア (ロジスティック回帰の線形結合)。モデル未配置・例外時は手調整スコアへフォールバック。
    戻り値: (score, details)
    """
    model = load_ml_model()
    if model is None:
        return compute_score(h, race_context, factor_table, cfg)
    try:
        f = _ml_features(h, race_context, factor_table, cfg)
        scale = model.get("display_scale", 10.0)
        contribs = []
        total = 0.0
        for name, mean, sd, coef in zip(model["features"], model["mean"],
                                        model["sd"], model["coef"]):
            if not sd:
                continue
            c = (f.get(name, 0.0) - mean) / sd * coef * scale
            total += c
            contribs.append((abs(c), name, c))
        contribs.sort(reverse=True)
        details = []
        for _, name, c in contribs[:6]:
            label = _ML_LABELS.get(name, name)
            extra = ""
            if name == "ln_odds":
                extra = f"(単勝{h.get('odds', '?')}倍)"
            elif name == "prev_rank":
                extra = f"({int(f['prev_rank'])}着)"
            details.append(f"ML {label}{extra}: {c:+.1f}")
        score = round(total, 1)
        details.append(f"合計(ML): {score:+.1f}")
        return score, details
    except Exception:
        return compute_score(h, race_context, factor_table, cfg)


# ─── WIN5 買い目配分 (荒れランク別カバレッジの貪欲最適化) ────────────────────

def allocate_picks(upset_ranks, coverage_map, budget, max_picks=8, fixed=None):
    """
    5レースの頭数配分を全探索で決定する。
    - upset_ranks: 各レースの荒れランク ["S","A","B","C" or None] (Noneはdefault扱い)
    - coverage_map: {"S": [k=1..Nのカバー率], ..., "default": [...]}
    - budget: 購入点数上限 (Π picks <= budget)
    - fixed: 1頭固定(軸)にするレースindexの集合 (点数を他レースに回す)
    戻り値: (picks[], est_hit_rate)  picks[i] = レースiの頭数
    Πk <= budget の制約下で Πcoverage (推定的中率) を最大化する。
    """
    import itertools as _it
    fixed = set(fixed or ())
    curves = []
    for r in upset_ranks:
        curve = coverage_map.get(r) or coverage_map.get("default") or [0.3, 0.5, 0.6, 0.7, 0.75, 0.8, 0.84, 0.87]
        curves.append(curve)
    n = len(upset_ranks)
    kmax = [1 if i in fixed else min(max_picks, len(c)) for i, c in enumerate(curves)]

    # 全探索 (最大8^5=32768通り): Πk <= budget の下で Πcoverage 最大
    best_picks, best_est = [1] * n, 0.0
    for combo in _it.product(*[range(1, km + 1) for km in kmax]):
        pts = 1
        for k in combo:
            pts *= k
        if pts > budget:
            continue
        est = 1.0
        for i, k in enumerate(combo):
            est *= curves[i][k - 1]
        # 同率なら点数の少ない方 (資金効率優先)
        if est > best_est + 1e-12 or (abs(est - best_est) <= 1e-12 and pts < _pts_of(best_picks)):
            best_picks, best_est = list(combo), est
    return best_picks, best_est


def allocate_picks_prob(prob_lists, budget, max_picks=8, fixed=None):
    """
    レース固有の勝率分布から頭数配分を全探索で決定する (荒れランク平均の代替)。
    - prob_lists: 各レースの「スコア順に並べた推定勝率」のリスト
      (conditional logit モデルの softmax 出力。合計≒1)
    - budget / fixed: allocate_picks と同じ
    戻り値: (picks[], est_hit_rate)
    Πk <= budget の制約下で Π(上位k頭の勝率和) を最大化する。
    """
    import itertools as _it
    fixed = set(fixed or ())
    curves = []
    for probs in prob_lists:
        cum, curve = 0.0, []
        for p in probs[:max_picks]:
            cum += max(float(p), 0.0)
            curve.append(min(cum, 1.0))
        curves.append(curve or [0.3])
    n = len(prob_lists)
    kmax = [1 if i in fixed else len(c) for i, c in enumerate(curves)]

    best_picks, best_est = [1] * n, 0.0
    for combo in _it.product(*[range(1, km + 1) for km in kmax]):
        pts = 1
        for k in combo:
            pts *= k
        if pts > budget:
            continue
        est = 1.0
        for i, k in enumerate(combo):
            est *= curves[i][k - 1]
        if est > best_est + 1e-12 or (abs(est - best_est) <= 1e-12 and pts < _pts_of(best_picks)):
            best_picks, best_est = list(combo), est
    return best_picks, best_est


def win_probs_from_ml_scores(scores):
    """MLスコア(表示スケール)のリスト → レース内勝率 (softmax)。
    conditional logit で学習したモデルのみ確率として妥当。"""
    import math as _m
    model = load_ml_model()
    if not scores or model is None or model.get("objective") != "conditional_logit":
        return None
    scale = model.get("display_scale", 10.0) or 10.0
    temp = model.get("prob_temperature", 1.0) or 1.0
    raw = [s / scale / temp for s in scores]
    mx = max(raw)
    e = [_m.exp(r - mx) for r in raw]
    z = sum(e)
    return [v / z for v in e] if z > 0 else None


def _pts_of(ks):
    p = 1
    for k in ks:
        p *= k
    return p


# ─── 発掘条件 (mine_criteria.py がOOS検証済みで出力) ─────────────────────────

_MINED_CACHE = {"rules": None}


def _load_mined_rules():
    """mined_rules.csv → {場: [rule]}。rule = {type, dist, conds[], kind, pts}"""
    if _MINED_CACHE["rules"] is not None:
        return _MINED_CACHE["rules"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_files", "common", "mined_rules.csv")
    rules = {}
    try:
        import csv as _csv
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for row in _csv.DictReader(f):
                try:
                    rules.setdefault(row["場"], []).append({
                        "type": row["芝ダ"], "dist": int(row["距離"]),
                        "conds": [c for c in (row.get("条件1"), row.get("条件2"),
                                              row.get("条件3")) if c and c.strip()],
                        "kind": row["種別"], "pts": float(row["点数"]),
                    })
                except (KeyError, ValueError, TypeError):
                    continue
    except Exception:
        pass
    _MINED_CACHE["rules"] = rules
    return rules


def eval_mined_rules(h, race_context, cfg):
    """発掘条件 (買い/消し) の該当チェック。分析エンジンは本番と同じ check_condition。
    該当ルールの点数×重みを合算 (±クランプ)。"""
    params = cfg.get("params", DEFAULT_CFG["params"])
    weights = cfg.get("weights", DEFAULT_CFG["weights"])
    w = weights.get("mined_rules", 0.0)
    if not w:
        return 0.0, []
    venue = race_context.get("venue")
    race_type = race_context.get("race_type") or race_context.get("type")
    dist = race_context.get("distance") or race_context.get("dist")
    rules = _load_mined_rules().get(venue)
    if not rules or not race_type or not dist:
        return 0.0, []
    try:
        import analysis
    except ImportError:
        return 0.0, []
    total, details = 0.0, []
    clamp = params.get("mined_clamp", 4.0)
    for rule in rules:
        if rule["type"] != race_type or rule["dist"] != int(dist):
            continue
        try:
            if all(analysis.check_condition(c, h, race_context, {}, {})
                   for c in rule["conds"]):
                pts = rule["pts"] * w * (1 if rule["kind"] == "買い" else -1)
                total += pts
                details.append(f"発掘条件[{rule['kind']}] {'×'.join(rule['conds'])} → {pts:+.1f}")
        except Exception:
            continue
    total = max(-clamp, min(clamp, total))
    return total, details[:4]


# ─── 枠×使用コース (コース辞典由来) ──────────────────────────────────────────

_WAKU_RAIL_CACHE = {"data": None}


def _load_waku_rail():
    """waku_rail_bias.json (extract_waku_rail.py が生成)。無ければ空dict。"""
    if _WAKU_RAIL_CACHE["data"] is not None:
        return _WAKU_RAIL_CACHE["data"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_files", "common", "waku_rail_bias.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        _WAKU_RAIL_CACHE["data"] = {k: v for k, v in data.items() if not k.startswith("_")}
    except Exception:
        _WAKU_RAIL_CACHE["data"] = {}
    return _WAKU_RAIL_CACHE["data"]


def eval_waku_rail(h, race_context, cfg):
    """当日の使用コース (A/B/C) に対応する枠グループ成績から加減点。
    pts = clamp((枠グループ複勝率 − 表内平均) × weight, ±clamp)
    使用コース不明・表なし・中間枠 (グループ範囲外) は 0。"""
    params = cfg.get("params", DEFAULT_CFG["params"])
    weights = cfg.get("weights", DEFAULT_CFG["weights"])
    w = weights.get("waku_rail", 0.0)
    rail = race_context.get("rail")
    w_num = h.get("w_num")
    if not w or not rail or not w_num:
        return 0.0, []
    venue = race_context.get("venue")
    race_type = race_context.get("race_type") or race_context.get("type")
    dist = race_context.get("distance") or race_context.get("dist")
    if not (venue and race_type and dist):
        return 0.0, []
    base_key = f"{'芝' if race_type == '芝' else 'ダート'}{dist}"
    course_map = _load_waku_rail().get(venue) or {}
    table = course_map.get(base_key)
    if table is None:  # 内/外回りの付記付きキーが1つだけならそれを採用
        cands = [v for k, v in course_map.items() if k.startswith(base_key)]
        table = cands[0] if len(cands) == 1 else None
    rows = (table or {}).get(str(rail).upper())
    if not rows:
        return 0.0, []

    group_show = None
    group_label = None
    for label, rates in rows.items():
        m = re.match(r"^(\d+)-(\d+)$", str(label))
        if m and int(m.group(1)) <= int(w_num) <= int(m.group(2)):
            group_show = rates.get("show")
            group_label = label
            break
    shows = [r.get("show") for r in rows.values() if r.get("show") is not None]
    if group_show is None or len(shows) < 2:
        return 0.0, []
    avg = sum(shows) / len(shows)
    clamp = params.get("waku_rail_clamp", 3.0)
    pts = max(-clamp, min(clamp, (group_show - avg) * w))
    if abs(pts) < 0.05:
        return 0.0, []
    return pts, [f"枠×{str(rail).upper()}コース {group_label}枠: "
                 f"複勝{group_show:.1f}% (表平均{avg:.1f}%) → {pts:+.1f}"]


# ─── 能力サブスコア (タイム指数 / クラス実績 / 上がり質) ────────────────────

_STD_TIMES_CACHE = {"data": None, "variants": None}


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


def _load_track_variants():
    """track_variants.json (日別・場別・芝ダ別の馬場差。正=時計がかかる馬場)"""
    if _STD_TIMES_CACHE["variants"] is not None:
        return _STD_TIMES_CACHE["variants"]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "data_files", "common", "track_variants.json")
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            _STD_TIMES_CACHE["variants"] = json.load(f).get("variants", {})
    except Exception:
        _STD_TIMES_CACHE["variants"] = {}
    return _STD_TIMES_CACHE["variants"]


def track_variant(date8, place, track):
    """指定日の馬場差 (秒)。データが無い日は 0.0。"""
    if not date8:
        return 0.0
    return _load_track_variants().get(str(date8), {}).get(place, {}).get(track, 0.0)


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
    canonical_track = str(run.get("track_type") or "")
    canonical_dist = run.get("distance")
    if place and canonical_track and canonical_dist:
        track = "芝" if canonical_track == "芝" else (
            "ダート" if canonical_track in ("ダ", "ダート") else None)
        try:
            dist = int(canonical_dist)
        except (TypeError, ValueError):
            dist = None
        if track and dist:
            cond = next((ch for ch in str(run.get("condition", "")) if ch in "良稍重不"), "")
            return (place, track, dist,
                    run.get("race_class") or class_label(run.get("race_name", "")), cond)
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

    # ── タイム指数 (利用可能な過去走の最良値、馬場差補正つき) ──
    use_variant = params.get("track_variant", False)
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
        # 馬場差 (その日の時計のかかり具合)。過去走の日付は raw から抽出
        variant = 0.0
        var_note = ""
        if use_variant:
            dm = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", str(run.get("raw", "")))
            if dm:
                date8 = f"{dm.group(1)}{int(dm.group(2)):02d}{int(dm.group(3)):02d}"
                variant = track_variant(date8, place, track)
                if variant:
                    var_note = f" 馬場差{variant:+.1f}"
        diff = base + variant - tsec  # 正 = その日の馬場を考慮しても基準勝ちタイムより速い
        rw = recency[i] if i < len(recency) else recency[-1]
        pts = max(-params.get("time_clamp", 8.0),
                  min(params.get("time_clamp", 8.0), diff * params.get("time_k", 2.0))) * rw
        label = (f"{['前走','2走前','3走前','4走前'][i]} {place}{track}{dist}"
                 f"({rclass}{cond or ''}) 基準{-diff:+.1f}秒{var_note}")
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

def _factor_points(row, baseline_show, params, weight, rate_key="show_rate"):
    """1ファクター分の得点。データ不足時は None。rate_key で複勝率/勝率ベースを切替。"""
    starts = row.get("starts") or 0
    show = row.get(rate_key)
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
    factor_table が None のコースでも能力・血統辞典・判定ボーナスは算出する。
    例外発生時、および算出根拠が1つも無い場合は (None, [])。
    """
    try:
        return _compute_score_inner(h, race_context, factor_table, cfg)
    except Exception:
        return None, []


def _compute_score_inner(h, race_context, factor_table, cfg):
    params = cfg.get("params", DEFAULT_CFG["params"])
    weights = cfg.get("weights", DEFAULT_CFG["weights"])
    style_map = cfg.get("style_map", DEFAULT_CFG["style_map"])
    rate_key = params.get("rate_key", "show_rate")
    rate_label = "勝" if rate_key == "win_rate" else "複"
    baseline_show = None
    if factor_table is not None:
        baseline_show = factor_table["baseline"].get(rate_key)
        if baseline_show is None:
            baseline_show = factor_table["baseline"]["show_rate"]
            rate_key, rate_label = "show_rate", "複"

    dist_val = race_context.get("dist") or race_context.get("dist_val") or 0
    race_type = race_context.get("type") or race_context.get("race_type") or ""
    prev_dist, prev_surf = _prev_course_info(h)

    total = 0.0
    details = []

    def add(factor, entity_label, row):
        w = weights.get(factor)
        # 重み0・データなし・基準値なし(factors未整備コース)は算入も表示もしない
        if not w or row is None or baseline_show is None:
            return
        pts, roi_note = _factor_points(row, baseline_show, params, w, rate_key)
        if pts is None:
            return
        nonlocal total
        total += pts
        details.append(
            f"{FACTOR_LABELS.get(factor, factor)} {entity_label}: "
            f"{rate_label}{row[rate_key]:.1f}% (基準{baseline_show:.1f}%) n={row['starts']}"
            f" → {pts:+.1f}{roi_note}"
        )

    # 種牡馬
    add("father_w", h.get("sire", "-"),
        _match_entity((factor_table or {}).get("father_w"), h.get("sire")))

    # 騎手
    add("jockey_w", h.get("jock", "-"),
        _match_entity((factor_table or {}).get("jockey_w"), h.get("jock")))

    # 枠順
    w_num = h.get("w_num")
    if w_num:
        fmap = (factor_table or {}).get("frame") or {}
        row = fmap.get(_norm(f"{w_num}枠")) or fmap.get(_norm(str(w_num)))
        add("frame", f"{w_num}枠", row)

    # 脚質 (複数該当は平均、重み0なら算入しない)
    styles = style_map.get(h.get("kyakushitsu", ""), []) if weights.get("averunningstyle") else []
    if styles:
        smap = (factor_table or {}).get("averunningstyle") or {}
        rows = [smap.get(_norm(s)) for s in styles]
        rows = [r for r in rows if r]
        if rows:
            w = weights.get("averunningstyle", 0)
            pts_list = []
            for r in rows:
                p, _ = _factor_points(r, baseline_show, params, w, rate_key)
                if p is not None:
                    pts_list.append((p, r))
            if pts_list:
                avg = sum(p for p, _ in pts_list) / len(pts_list)
                total += avg
                label = "/".join(styles)
                shows = "/".join(f"{r[rate_key]:.1f}" for _, r in pts_list)
                details.append(
                    f"{FACTOR_LABELS['averunningstyle']} {label}: {rate_label}{shows}% "
                    f"(基準{baseline_show:.1f}%) → {avg:+.1f}"
                )

    # 距離変更 (前走距離との実差分 → db-keibaの7段階ラベル)
    d_label = _distance_label(dist_val, prev_dist)
    if d_label:
        dmap = (factor_table or {}).get("distance") or {}
        add("distance", d_label, dmap.get(_norm(d_label)))

    # コース替わり (前走芝/ダ → 今回芝/ダ)
    if prev_surf:
        cur = "芝" if race_type == "芝" else "ダ"
        s_label = f"{prev_surf}→{cur}"
        smap = (factor_table or {}).get("surface") or {}
        add("surface", s_label, smap.get(_norm(s_label)))

    # 所属 (美浦/栗東)
    affi = h.get("affi")
    if affi in ("美浦", "栗東"):
        tmap = (factor_table or {}).get("stable_trainer") or {}
        add("stable_trainer", affi, tmap.get(_norm(affi)))

    # 枠×使用コース (コース辞典の A/B/C コース別枠成績。書籍集計値のため控えめな重み)
    wr_pts, wr_details = eval_waku_rail(h, race_context, cfg)
    if wr_details:
        total += wr_pts
        details.extend(wr_details)

    # 発掘条件 (mine_criteria.py がOOS検証済みで出力した買い/消しルール)
    mr_pts, mr_details = eval_mined_rules(h, race_context, cfg)
    if mr_details:
        total += mr_pts
        details.extend(mr_details)

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

    # 道悪適性 (当日馬場が稍/重/不のときのみ)
    wet_pts, wet_details = eval_wet_aptitude(h, race_context, cfg)
    if wet_details:
        total += wet_pts
        details.extend(wet_details)

    # 市場 (単勝オッズ) — odds_k > 0 の設定時のみ (WIN5用)
    mk_pts, mk_details = eval_market(h, cfg)
    if mk_details:
        total += mk_pts
        details.extend(mk_details)

    # 既存◎〇△判定ボーナス (ルール別重みを反映: グレード点 × 該当ルールの重み の最大値)
    gb = params.get("grade_bonus", {})
    matches = h.get("ultra_matches") or []
    if matches:
        best = max(matches, key=lambda m: gb.get(m.get("grade", ""), 0.0) * m.get("weight", 1.0))
        bw = best.get("weight", 1.0)
        pts = gb.get(best.get("grade", ""), 0.0) * bw
        if pts:
            total += pts
            w_txt = f", 重み{bw:.2f}" if abs(bw - 1.0) >= 0.005 else ""
            details.append(f"判定{best['grade']} (項番{best['id']}{w_txt}) → {pts:+.1f}")
    else:
        # ultra_matches 未対応の呼び出し元 (バックテスト等) 向けフォールバック
        grade = h.get("grade", "")
        if grade in gb and gb[grade]:
            total += gb[grade]
            details.append(f"判定{grade} → {gb[grade]:+.1f}")

    # 好走条件の該当数ボーナス (2件目以降を加点。バックテストで該当数と勝率の単調増加を確認済。
    # ルール別重みがある場合は2件目以降の重み合計で加点)
    cc = params.get("criteria_count", {})
    if cc.get("per_extra"):
        if matches:
            if len(matches) > 1:
                w_sorted = sorted((m.get("weight", 1.0) for m in matches), reverse=True)
                pts = min(cc.get("cap", 6.0), cc["per_extra"] * sum(w_sorted[1:]))
                total += pts
                details.append(f"好走条件 該当{len(matches)}件 → {pts:+.1f}")
        else:
            n_match = len(h.get("ultra_details") or [])
            if n_match > 1:
                pts = min(cc.get("cap", 6.0), cc["per_extra"] * (n_match - 1))
                total += pts
                details.append(f"好走条件 該当{n_match}件 → {pts:+.1f}")

    if not details:  # 算出根拠が1つも無い (履歴なし かつ factors未整備 等)
        return None, []

    score = round(total, 1)
    details.append(f"合計: {score:+.1f}")
    return score, details
