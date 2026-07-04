"""
競馬血統総辞典 買い&消し条件のルールコンパイラ
================================================
data_files/common/sire_buysell.csv の自由文条件を構造化ルール
(data_files/common/sire_buysell_rules.json) にコンパイルする。

文の構造:
  " or " 区切り = OR (いずれかの節が成立すればルール成立)
  節内の 。/・/で/の 等に埋め込まれた述語 = AND

判定不能な句 (母父の型分類・キャリア数 等) は unknown_notes に保持し、
実行時は「要確認」として表示のみ行う (加点はする/しないを設定で制御)。

【使い方】 python compile_buysell_rules.py   # 変換+未解釈句レポート
"""
import csv
import json
import os
import re
import sys
import unicodedata

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE_DIR, "api", "data_files", "common", "sire_buysell.csv")
DST = os.path.join(BASE_DIR, "api", "data_files", "common", "sire_buysell_rules.json")

VENUES = ["東京", "中山", "阪神", "京都", "中京", "新潟", "福島", "小倉", "札幌", "函館"]


def nfkc(s):
    return unicodedata.normalize("NFKC", s)


def parse_segment(seg):
    """OR節1つ → 述語dict。解釈できた語句は seg から除去していき、残渣を unknown に。"""
    p = {}
    unknown = []
    s = nfkc(seg).strip().rstrip("。")

    def eat(pattern, flags=0):
        nonlocal s
        m = re.search(pattern, s, flags)
        if m:
            s = (s[:m.start()] + "◆" + s[m.end():])
            return m
        return None

    # ── 注記 (特に〜) は非拘束の強調として note へ ──
    m = eat(r"特に[^。・◆]*")
    if m:
        p["note"] = m.group(0)

    # ── 直線 ──
    if eat(r"直線[がのは]?長い(コース)?の?"):
        p["straight"] = "long"
    elif eat(r"直線[がのは]?短い(コース)?の?"):
        p["straight"] = "short"

    # ── 距離 (表面より先に: "芝1800m・2200m・2500m以上" 等) ──
    m = eat(r"(\d{3,4})m?[〜~-](\d{3,4})m")
    if m:
        p["dist_min"], p["dist_max"] = int(m.group(1)), int(m.group(2))
    else:
        m = eat(r"((?:\d{3,4}m?[・、]\s?)+)(\d{3,4})m以上")
        if m:  # 列挙 + 最後が以上
            p["dist_list"] = [int(x) for x in re.findall(r"\d{3,4}", m.group(1))]
            p["dist_min_alt"] = int(m.group(2))
        else:
            m = eat(r"(\d{3,4})m以上")
            if m:
                p["dist_min"] = int(m.group(1))
            m = eat(r"(\d{3,4})m以下")
            if m:
                p["dist_max"] = int(m.group(1))
            m = eat(r"(\d{3,4})m未満")
            if m:
                p["dist_max"] = int(m.group(1)) - 100

    # ── 場所 ──
    for v in VENUES:
        if eat(v):
            p["venue"] = v
            break

    # ── 表面 ──
    if eat(r"ダート|ダ(?=\d|で|の|。|$)"):
        p["surface"] = "ダート"
    if eat(r"芝"):
        p["surface"] = "芝" if "surface" not in p else p["surface"]

    # ── 性別 (組合せ対応) ──
    sexes = []
    if eat(r"牡馬[、,]\s*セン馬|セン馬[、,]\s*牡馬"):
        sexes = ["牡", "セ"]
    else:
        if eat(r"牡馬"):
            sexes.append("牡")
        if eat(r"セン馬"):
            sexes.append("セ")
        if eat(r"牝馬"):
            sexes.append("牝")
    if sexes:
        p["sex"] = sexes

    # ── 枠 ──
    if eat(r"外枠\s*\(?[55]\s*[〜~-]\s*[88]枠\)?|外枠"):
        p["waku_min"] = 5
    if eat(r"内枠\s*\(?[11]\s*[〜~-]\s*[44]枠\)?|内枠"):
        p["waku_max"] = 4

    # ── 馬体重 ──
    m = eat(r"大型馬\s*\(前走馬体重(\d{3})キロ以上\)|前走馬体重(\d{3})キロ以上")
    if m:
        p["prev_weight_min"] = int(m.group(1) or m.group(2))
    m = eat(r"小型馬\s*\(前走馬体重(\d{3})キロ未満\)|前走馬体重(\d{3})キロ未満")
    if m:
        p["prev_weight_max"] = int(m.group(1) or m.group(2)) - 1

    # ── 距離延長 / 前走逃げ切り ──
    if eat(r"前走から距離延長"):
        p["dist_extend"] = True
    if eat(r"前走逃げ切り好走"):
        p["nigekiri_prev"] = True

    # ── テン/上がりパターン (近似判定: 直近走の位置取り・上がり順位比率) ──
    m = eat(r"近走で?先行(していない|経験の?ない)馬\s*\(テンパターン(\d+)以内を未経験\)|テンパターン(\d+)以内を未経験")
    if m:
        p["senko_never"] = int(m.group(2) or m.group(3)) / 100.0
    else:
        m = eat(r"近走先行経験馬?\s*\(テンパターン(\d+)以内\)|近走先行経験(のない馬)?")
        if m:
            if m.group(2):  # 経験のない馬
                p["senko_never"] = 0.5
            else:
                p["senko_recent"] = int(m.group(1)) / 100.0 if m.group(1) else 0.5
    m = eat(r"近走上がり上位馬\s*\(上がりパターン(\d+)以内\)")
    if m:
        p["agari_recent"] = int(m.group(1)) / 100.0

    # ── クラス条件 ──
    if eat(r"新馬戦?"):
        p["class_cond"] = "shinba"
    elif eat(r"古馬混合戦?"):
        p["class_cond"] = "kobakongo"
    elif eat(r"[22][〜~、,][33]歳限定戦?|[22]、[33]歳限定戦?"):
        p["class_cond"] = "nisai_sansai"
    elif eat(r"下級条件\s*\([11]勝クラスより下\)"):
        p["class_cond"] = "below_1win"
    elif eat(r"[22]勝クラス以上"):
        p["class_cond"] = "ge_2win"
    elif eat(r"条件戦"):
        p["class_cond"] = "joken"

    # ── 母父 ──
    m = eat(r"母父が?([^。・◆]*?)(以外)?(?=[。・◆での]|$)")
    if m:
        bms_txt, negate = m.group(1).strip(), bool(m.group(2))
        grp = None
        if "非サンデー系" in bms_txt:
            grp, negate = "sunday_all", True
        elif re.search(r"ディープ系|Tサンデー系", bms_txt):
            grp = "deep_tsunday"
        elif re.search(r"サンデー系", bms_txt):
            grp = "sunday_all"
        if grp:
            p["bms_group"] = {"group": grp, "negate": negate}
        else:
            unknown.append(f"母父{bms_txt}{'以外' if negate else ''}")

    # ── キャリア ──
    m = eat(r"キャリア(\d+)戦以内")
    if m:
        unknown.append(f"キャリア{m.group(1)}戦以内")

    # ── 残渣 → unknown ──
    residue = re.sub(r"[◆。・、,\s]+|の|で|or", "", s)
    if residue:
        unknown.append(residue)
    if unknown:
        p["unknown_notes"] = unknown
    return p


def compile_rules():
    rules = []
    problems = []
    with open(SRC, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            sire = row["種牡馬名"].strip()
            kubun = row["区分"].strip()
            text = row["条件文"].strip()
            if not sire or kubun not in ("買い", "消し"):
                continue
            segments = [parse_segment(seg) for seg in re.split(r"\s+or\s+", nfkc(text))]
            # 全セグメントが判定可能述語ゼロなら実行時スキップ対象
            evaluable_keys = {"surface", "venue", "dist_min", "dist_max", "dist_list",
                              "dist_min_alt", "sex", "waku_min", "waku_max",
                              "prev_weight_min", "prev_weight_max", "dist_extend",
                              "nigekiri_prev", "senko_recent", "senko_never",
                              "agari_recent", "class_cond", "straight", "bms_group"}
            has_eval = any(any(k in seg for k in evaluable_keys) for seg in segments)
            rule = {"sire": sire, "kubun": kubun, "text": text, "segments": segments}
            rules.append(rule)
            if not has_eval:
                problems.append(f"[判定可能述語なし] {sire} {kubun}: {text}")
            for seg in segments:
                for u in seg.get("unknown_notes", []):
                    problems.append(f"[要確認句] {sire} {kubun}: {u}")

    out = {
        "version": 1,
        "source": "競馬血統総辞典 (sire_buysell.csv)",
        "straight_map_note": "直線長短は scoring.py の STRAIGHT_MAP で判定",
        "rules": rules,
    }
    with open(DST, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f"コンパイル完了: {len(rules)}ルール -> {DST}")
    n_seg = sum(len(r["segments"]) for r in rules)
    n_unknown = sum(1 for r in rules for s in r["segments"] if s.get("unknown_notes"))
    print(f"セグメント数: {n_seg} / 要確認句を含むセグメント: {n_unknown}")
    if problems:
        print("\n----- 未解釈・要確認レポート -----")
        for p in problems:
            print("  " + p)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    compile_rules()
