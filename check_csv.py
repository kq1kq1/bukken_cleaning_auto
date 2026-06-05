"""
自社公開物件CSV vs REINS物件DB 照合ツール

使い方:
  実行.bat をダブルクリック → CSVファイルを選択 → 自動照合・メール通知
"""

import json
import sys
import re
import logging
import unicodedata
from pathlib import Path
from datetime import datetime
from difflib import SequenceMatcher
from collections import defaultdict

import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox

from mailer import send_email, build_check_email

# ----------------------------------------------------------------
# パス・定数
# ----------------------------------------------------------------

BASE_DIR      = Path(__file__).parent
CONFIG_PATH   = BASE_DIR / "config.json"
COL_MAP_PATH  = BASE_DIR / "column_map.json"
LOG_PATH      = BASE_DIR / "checker.log"
REPORT_DIR    = BASE_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# CSV列名の候補 → 標準フィールド名（_で始まるものは preprocess_csv が生成する派生列）
FIELD_ALIASES: dict[str, list[str]] = {
    "物件番号": ["物件番号", "管理番号", "reins番号", "レインズ番号", "番号"],
    "物件管理番号": ["物件管理番号", "管理番号", "自社管理番号"],
    "建物名":   ["建物名", "物件名", "マンション名", "物件名称", "名称", "_建物名_clean"],
    "所在地":   ["所在地", "住所", "物件所在地", "_所在地"],
    "価格":     ["価格", "販売価格", "売出価格", "物件価格", "金額"],
    "会社名":   ["会社名", "元付会社", "取扱会社", "業者名", "元付業者名"],
    "物件種別": ["物件種別", "種別", "取引種別", "_物件種別"],
    "所在階":   ["所在階", "階数", "住戸階", "所属階_From_階数"],
    "土地面積": ["土地面積", "敷地面積", "土地面積_面積_平米"],
    "建物面積": ["建物面積", "延床面積", "建物面積_面積_平米"],
    "専有面積": ["専有面積", "占有面積", "専有面積_面積_平米"],
    "交通":     ["交通", "徒歩", "駅徒歩", "_交通"],
    "沿線駅":   ["沿線駅", "最寄駅", "交通手段1_駅"],
    "間取り":   ["間取り", "間取", "_間取り"],
}

# CSV「種別」→ DB「物件種別」の対応
_TYPE_MAP = {
    "マンション":  "中古マンション",
    "一戸建て":    "中古戸建",
    "一戸建":     "中古戸建",
    "戸建て":     "中古戸建",
    "戸建":       "中古戸建",
    "土地":       "売地",
    "売地":       "売地",
    "新築戸建":   "新築戸建",
    "新築一戸建": "新築戸建",
}

# 漢数字・旧字体 → アラビア数字
_KANJI_NUM = str.maketrans("一二三四五六七八九壱弐参", "123456789123")


# ----------------------------------------------------------------
# 正規化ユーティリティ
# ----------------------------------------------------------------

def normalize(s) -> str:
    """
    照合用の完全正規化。
    - NFKC（全角数字/英字→半角、半角カナ→全角、ローマ数字→アルファベット）
    - 漢数字→アラビア数字
    - ハイフン類・記号類（・★☆◯〇）を統一
    - 連続ハイフン → 1つに集約（例: "プレジャー・ガーデン" と "プレジャーガーデン" を同一視）
    - スペース除去
    - 小文字化
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.translate(_KANJI_NUM)
    # ハイフン・中点・記号類を統一
    s = re.sub(r"[‐‑‒–—ーｰ･・★☆◯〇○●◎◇◆□■△▲▽▼]", "-", s)
    s = re.sub(r"-+", "-", s)
    s = re.sub(r"[\s　]+", "", s)
    return s.lower()


def addr_key(addr: str) -> str:
    """
    所在地から「都道府県+市区町村+丁目」レベルのキーを生成。
    DB は丁目まで、CSV は番地まであるため、丁目で揃える。
    """
    n = normalize(addr)
    # "N丁目" があればそこまで
    m = re.search(r"^(.*?\d+丁目)", n)
    if m:
        return m.group(1)
    # 丁目なし: 市区町村名 + 地名（番地・数字の前まで）
    m = re.match(r"^(.*?[市区町村])([^市区町村]*)", n)
    if m:
        city = m.group(1)
        rest = re.split(r"\d", m.group(2))[0]
        return city + rest if rest else city
    return n[:20]


def parse_price(s) -> float | None:
    """価格文字列を万円単位の float に変換（円単位にも対応）"""
    n = unicodedata.normalize("NFKC", str(s)).replace(",", "").replace("、", "")
    n = re.sub(r"[^\d.]", "", n)
    if not n:
        return None
    try:
        v = float(n)
        if v >= 10_000_000:   # 1億以上 → 円単位とみなして万円に変換
            v /= 10_000
        return v
    except ValueError:
        return None


def within_recent_days(date_str, days: int) -> bool:
    """
    日付文字列が「実行日からdays日以内」かを判定する。
    成約・取消日が古い（=前回以前のクリーニングで確定したもの）を除外するために使う。
    パースできない・空の場合は False（=対象外）。
    """
    s = str(date_str).strip()
    if not s:
        return False
    s = unicodedata.normalize("NFKC", s)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            d = datetime.strptime(s, fmt).date()
            delta = (datetime.now().date() - d).days
            # 未来日のわずかなズレは許容（-2日）、過去はdays日以内
            return -2 <= delta <= days
        except ValueError:
            continue
    return False


def parse_area(s) -> float | None:
    """面積文字列を float（㎡）に変換"""
    n = unicodedata.normalize("NFKC", str(s))
    n = re.sub(r"[^\d.]", "", n.split("(")[0].split("（")[0])
    try:
        return float(n) if n else None
    except ValueError:
        return None


def areas_match(a, b, tol: float = 0.02) -> bool:
    """面積の一致判定（デフォルト±2%以内）"""
    va, vb = parse_area(a), parse_area(b)
    if not va or not vb:
        return False
    return abs(va - vb) / max(va, vb) <= tol


def areas_exact(a, b) -> bool:
    """面積の厳密一致判定（±0.5%以内：CSVとDBの小数点丸め差を吸収する程度）"""
    return areas_match(a, b, tol=0.005)


def land_area_strict_eq(a, b) -> bool:
    """
    土地面積の厳密一致判定（一般媒介の戸建・土地用）。

    判定ルール:
      - 1の位（整数部）が完全一致 → 必須
      - 両方に小数点以下が入っている場合のみ、小数点以下も一致が必要
      - 片方が整数のみ・もう片方に小数点以下がある場合は整数部一致のみで OK
        （REINS では小数点以下が省略されている可能性があるため）

    例:
      100  vs 100    → True
      100  vs 100.5  → True  （片方のみ小数あり）
      100.5 vs 100.5 → True
      100.3 vs 100.5 → False （両方小数あり、不一致）
      100  vs 101    → False （整数部不一致）
    """
    va, vb = parse_area(a), parse_area(b)
    if va is None or vb is None:
        return False
    if int(va) != int(vb):
        return False
    da = va - int(va)
    db_ = vb - int(vb)
    # 両方に小数点以下が存在する場合のみ、その値も一致を要求
    if da > 0 and db_ > 0:
        return abs(da - db_) < 0.01
    return True


def floor_eq(a: str, b: str) -> bool:
    """
    所在階の一致判定。「3」「3階」「3F」「3f」すべて同じ階として扱う。
    どちらかが空なら False。
    """
    def num(s: str) -> str:
        m = re.match(r"(\d+)", normalize(str(s)))
        return m.group(1) if m else ""
    na, nb = num(a), num(b)
    return bool(na and nb and na == nb)


def station_match(
    csv_stations: list[tuple],
    db_sensen: str,
    db_kotsuu: str,
) -> bool:
    """
    CSV の交通手段1〜3（構造化済み）と DB の沿線駅 + 交通 が一致するか確認。

    照合モード:
      - 徒歩モード: 駅名 + 駅徒歩分数 が完全一致
      - バスモード: 駅名 + バス停徒歩分数 + バス乗車分数 が完全一致
        DB側 "停歩 X分/バス Y分" / CSV側 「区分=バス」+ バス停徒歩分・バス乗車分

    csv_stations の各要素: (路線名, 駅名, モード, 主時間, バス時間)
      - モード="walk": 主時間=駅徒歩分, バス時間=0
      - モード="bus":  主時間=バス停徒歩分, バス時間=バス乗車分

    駅名のプレフィックス対応:
      CSVの駅名がDBの駅名で終わり、かつプレフィックスがCSV or DBの路線名に含まれる
      場合も一致とみなす（例: CSV "京成8幡" / DB "8幡" で路線名が "京成"）
    """
    db_raw_parts = str(db_sensen).split()
    db_sta  = normalize(db_raw_parts[-1]).rstrip("駅") if db_raw_parts else ""
    db_line = normalize(db_raw_parts[0]) if len(db_raw_parts) > 1 else ""

    if not db_sta:
        return False

    # DBの交通フィールドを解析してモードと分数を抽出
    kotsuu_str = str(db_kotsuu)
    db_mode: str | None
    db_walk = -1
    db_bus  = -1
    if "停歩" in kotsuu_str and "バス" in kotsuu_str:
        wm = re.search(r"停歩\s*(\d+)", kotsuu_str)
        bm = re.search(r"バス\s*(\d+)", kotsuu_str)
        if wm and bm:
            db_mode, db_walk, db_bus = "bus", int(wm.group(1)), int(bm.group(1))
        else:
            db_mode = None
    elif kotsuu_str.strip():
        wm = re.search(r"(\d+)", kotsuu_str)
        if wm:
            db_mode, db_walk = "walk", int(wm.group(1))
        else:
            db_mode = "noinfo"
    else:
        db_mode = "noinfo"

    for st in csv_stations:
        csv_line, csv_sta, csv_mode, csv_main, csv_bus = st
        # 駅名の一致チェック
        if csv_sta == db_sta:
            name_ok = True
        elif csv_sta.endswith(db_sta) and len(db_sta) >= 2:
            prefix = csv_sta[: -len(db_sta)]
            name_ok = bool(prefix and (prefix in csv_line or prefix in db_line))
        else:
            name_ok = False
        if not name_ok:
            continue

        # DB側に時間情報なし → 駅名一致のみで通過
        if db_mode in (None, "noinfo"):
            return True
        # モード一致が必須（徒歩 vs バスは別の通勤経路）
        if csv_mode != db_mode:
            continue
        if db_mode == "walk":
            if csv_main == db_walk:
                return True
        elif db_mode == "bus":
            if csv_main == db_walk and csv_bus == db_bus:
                return True
    return False


def _csv_station_summary(csv_row: dict) -> str:
    """
    CSV の交通手段1〜3 を「路線 駅 徒歩/バスN分」の読みやすい文字列に整形（通知表示用）。
    最大2件まで「 / 」で連結。
    """
    parts = []
    for i in range(1, 4):
        line = str(csv_row.get(f"交通手段{i}_沿線", "") or "").strip()
        sta  = str(csv_row.get(f"交通手段{i}_駅", "") or "").strip()
        kubun = str(csv_row.get(f"交通手段{i}_所要時間_区分", "") or "")
        walk_sta = str(csv_row.get(f"交通手段{i}_所要時間_駅徒歩（分）", "") or "").strip()
        walk_bus = str(csv_row.get(f"交通手段{i}_所要時間_バス停徒歩（分）", "") or "").strip()
        bus_ride = str(csv_row.get(f"交通手段{i}_バス乗車（分）", "") or "").strip()
        if not sta:
            continue
        if "バス" in kubun and walk_bus and bus_ride:
            t = f"バス{bus_ride}分+徒歩{walk_bus}分"
        elif walk_sta:
            t = f"徒歩{walk_sta}分"
        else:
            t = ""
        parts.append(f"{line}{sta}駅 {t}".strip())
    return " / ".join(parts[:2])


def _db_station_summary(db_row: dict) -> str:
    """DBの沿線駅＋交通を読みやすい1行に整形（比較表示用）。"""
    sensen = str(db_row.get("沿線駅", "") or "").strip()
    kotsuu = str(db_row.get("交通", "") or "").strip()
    return f"{sensen} {kotsuu}".strip()


def _build_csv_stations(csv_row: dict) -> list[tuple]:
    """
    CSV の 交通手段1〜3 から構造化データを生成。

    返り値: [(路線名, 駅名, モード, 主時間, バス時間), ...]
      - モード="walk": 主時間=駅徒歩分, バス時間=0
      - モード="bus":  主時間=バス停徒歩分, バス時間=バス乗車分

    判定基準: 「所要時間_区分」列に "バス" が含まれる場合はバスモード、
    それ以外は徒歩モードとする。
    """
    result = []
    for i in range(1, 4):
        raw_line = str(csv_row.get(f"交通手段{i}_沿線", "") or "")
        raw_sta  = str(csv_row.get(f"交通手段{i}_駅", "") or "")
        kubun    = str(csv_row.get(f"交通手段{i}_所要時間_区分", "") or "")
        walk_sta = str(csv_row.get(f"交通手段{i}_所要時間_駅徒歩（分）", "") or "")
        walk_bus = str(csv_row.get(f"交通手段{i}_所要時間_バス停徒歩（分）", "") or "")
        bus_ride = str(csv_row.get(f"交通手段{i}_バス乗車（分）", "") or "")

        line_n = normalize(raw_line)
        sta_n  = normalize(raw_sta).rstrip("駅")
        if not sta_n:
            continue

        if "バス" in kubun:
            wm = re.search(r"(\d+)", walk_bus)
            bm = re.search(r"(\d+)", bus_ride)
            if wm and bm:
                result.append((line_n, sta_n, "bus", int(wm.group(1)), int(bm.group(1))))
        else:
            wm = re.search(r"(\d+)", walk_sta)
            if wm:
                result.append((line_n, sta_n, "walk", int(wm.group(1)), 0))
    return result


def names_match(a: str, b: str) -> bool:
    """
    建物名の一致判定。
    正規化後に完全一致 or 一方が他方を含む or 類似度0.85以上。
    """
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    # 長い方に短い方が含まれる（号棟違い等）
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 4 and short in long_:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= 0.85


_INDUSTRY_KEYWORDS = (
    "不動産", "建設", "ハウジング", "ホ-ムズ", "ホ-ム", "リアルティ", "住宅",
    "リフォ-ム", "リアルエステ-ト", "カンパニ-", "ハウス", "サ-ビス",
    "エステ-ト", "工務店", "コ-ポレ-ション", "プランニング", "商事",
    "リバブル", "リアルエステイト", "プロパティ",
)
_CORE_RE = re.compile(r"^(.*?(?:" + "|".join(_INDUSTRY_KEYWORDS) + r"))")


def _clean_company(s: str) -> str:
    """
    会社名から照合に不要な情報を除去する。

    除去対象:
      - 電話番号（先頭・末尾、ハイフン区切り）
      - 棟/号棟/号地/区画 情報（分譲地）
      - 法人格（株式会社/合同会社/有限会社 等）
      - 営業所/支店/出張所/センター/事務所 等のサフィックス
      - 「担当:○○」「TEL○○」等の付加情報

    ※「一建設」など、漢数字を含む会社名が電話番号と隣接した場合に
      漢数字→アラビア数字の変換が先に走ると電話番号と一体化して消える
      問題があるため、本関数内では NFKC + 記号正規化のみ先に行い、
      漢数字変換は電話番号除去の後に行う。
    """
    # NFKC + 記号統一（漢数字変換はまだしない）
    n = unicodedata.normalize("NFKC", str(s))
    n = re.sub(r"[‐‑‒–—ーｰ･・]", "-", n)
    n = re.sub(r"[\s　]+", "", n)
    n = n.lower()
    # 電話番号: 0で始まる10〜13桁（ハイフン区切り）。漢字の前にあるケースに対応
    n = re.sub(r"0\d[\d\-]{8,13}", "", n)
    # 漢数字をここで変換
    n = n.translate(_KANJI_NUM)
    # 棟・区画情報: "C号棟/全2棟", "1号地/全2号地", "全1棟", "2区画/", "B区画/" 等
    n = re.sub(r"[a-z]?号(棟|地)[/／].*", "", n)
    n = re.sub(r"\d+号(棟|地)[/／].*", "", n)
    n = re.sub(r"[/／]\d+(棟|地|区画).*", "", n)
    n = re.sub(r"[\da-z]+(棟|地|区画)[/／].*", "", n)   # "1区画/", "B区画/" パターン
    n = re.sub(r"全\d+(棟|区画|号地).*", "", n)
    n = re.sub(r"全[\da-z]+(棟|区画|号地).*", "", n)
    n = re.sub(r"\d+(棟|区画)$", "", n)
    # 末尾の単独数字（棟番号が漢字なしで付く場合: "旭ハウジング2" → "旭ハウジング"）
    n = re.sub(r"\d+$", "", n)
    # 担当者・TEL等の付加情報
    n = re.sub(r"担当[：:・]?.*", "", n)
    n = re.sub(r"tel[：:・]?.*", "", n)
    # 法人格マーカー
    n = re.sub(r"(株式会社|合同会社|有限会社|一般社団法人|（株）|（同）|（有）|\(株\)|\(同\)|\(有\)|㈱|㈲)", "", n)
    # 営業所・支店等のサフィックス
    n = re.sub(r"[\s　]*(営業所|支店|出張所|センター|事務所|本店|本社).*", "", n)
    return n.strip()


_KATA_ROMAJI = {
    "ア":"a","イ":"i","ウ":"u","エ":"e","オ":"o",
    "カ":"ka","キ":"ki","ク":"ku","ケ":"ke","コ":"ko",
    "サ":"sa","シ":"si","ス":"su","セ":"se","ソ":"so",
    "タ":"ta","チ":"ti","ツ":"tu","テ":"te","ト":"to",
    "ナ":"na","ニ":"ni","ヌ":"nu","ネ":"ne","ノ":"no",
    "ハ":"ha","ヒ":"hi","フ":"hu","ヘ":"he","ホ":"ho",
    "マ":"ma","ミ":"mi","ム":"mu","メ":"me","モ":"mo",
    "ヤ":"ya","ユ":"yu","ヨ":"yo",
    "ラ":"ra","リ":"ri","ル":"ru","レ":"re","ロ":"ro",
    "ワ":"wa","ヲ":"wo","ン":"n",
    "ガ":"ga","ギ":"gi","グ":"gu","ゲ":"ge","ゴ":"go",
    "ザ":"za","ジ":"ji","ズ":"zu","ゼ":"ze","ゾ":"zo",
    "ダ":"da","ヂ":"di","ヅ":"du","デ":"de","ド":"do",
    "バ":"ba","ビ":"bi","ブ":"bu","ベ":"be","ボ":"bo",
    "パ":"pa","ピ":"pi","プ":"pu","ペ":"pe","ポ":"po",
    "ヴ":"v","ッ":"",
    "ァ":"a","ィ":"i","ゥ":"u","ェ":"e","ォ":"o",
    "ャ":"ya","ュ":"yu","ョ":"yo",
}


def _kata_to_romaji(s: str) -> str:
    """カタカナ列を簡易ローマ字に変換（音訳近似のための簡易マップ）。"""
    return "".join(_KATA_ROMAJI.get(c, c) for c in s)


def _company_core(cleaned: str) -> str:
    """
    クリーニング済み会社名から「業種キーワードまで」のコア部分を抽出する。
    例:
      "1建設柏" → "1建設"
      "ヤマダホ-ムズ船橋店" → "ヤマダホ-ムズ"
      "ケイアイスタ-不動産北千住" → "ケイアイスタ-不動産"
    抽出できなければ元の文字列を返す。
    """
    m = _CORE_RE.match(cleaned)
    return m.group(1) if m else cleaned


def company_match(a: str, b: str) -> bool:
    """
    会社名の一致判定。

    判定手順:
      1. クリーニング後の文字列で完全一致 / 一方が他方を含む / 類似度0.75以上
      2. それでも不一致なら「業種キーワードまでのコア部分」を抽出して比較
         （「1建設柏」「1建設松戸」のように営業地域が違うだけのケースを救う）
    """
    na, nb = _clean_company(a), _clean_company(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 4 and short in long_:
        return True
    if SequenceMatcher(None, na, nb).ratio() >= 0.75:
        return True
    # コア部分（業種キーワードまで）で比較
    ca, cb = _company_core(na), _company_core(nb)
    if ca and cb and len(ca) >= 3 and len(cb) >= 3 and ca == cb:
        return True
    # カタカナ↔英字の音訳近似フォールバック（"living-net" vs "リビング-ネット" 等）
    ra = _kata_to_romaji(na)
    rb = _kata_to_romaji(nb)
    if (ra != na or rb != nb) and SequenceMatcher(None, ra, rb).ratio() >= 0.7:
        return True
    return False


def type_group(t: str) -> str:
    """物件種別を大分類に変換"""
    t = str(t)
    if "マンション" in t:
        return "mansion"
    if "戸建" in t or "一戸建" in t:
        return "kodate"
    if "土地" in t or "売地" in t:
        return "tochi"
    return "other"


# ----------------------------------------------------------------
# CSV前処理
# ----------------------------------------------------------------

def preprocess_csv(df: pd.DataFrame) -> pd.DataFrame:
    """
    自社管理システムCSVの分割列を結合し、照合用の派生列を追加する。
    追加列: _所在地 / _交通 / _間取り / _物件種別 / _建物名_clean
    """
    df = df.copy()

    # 所在地を結合
    addr_parts = ["所在地_都道府県", "所在地_市区町村", "所在地_町域・丁目", "所在地_番地"]
    if "所在地_都道府県" in df.columns:
        df["_所在地"] = df.apply(
            lambda r: "".join(str(r.get(c, "") or "") for c in addr_parts if c in df.columns),
            axis=1,
        )

    # 交通情報を結合（交通手段1〜3）
    # _交通N = "路線名駅名徒歩N分" の連結文字列（照合用・表示用兼用）
    for _i in range(1, 4):
        _col_line = f"交通手段{_i}_沿線"
        _col_sta  = f"交通手段{_i}_駅"
        _col_walk = f"交通手段{_i}_所要時間_駅徒歩（分）"
        if _col_sta in df.columns or _col_line in df.columns:
            def _mk_traffic(r, _n=_i):
                line = str(r.get(f"交通手段{_n}_沿線", "") or "")
                sta  = str(r.get(f"交通手段{_n}_駅", "") or "")
                walk = str(r.get(f"交通手段{_n}_所要時間_駅徒歩（分）", "") or "")
                parts = [p for p in [line, sta] if p]
                if walk:
                    parts.append(f"徒歩{walk}分")
                return "".join(parts)
            df[f"_交通{_i}"] = df.apply(_mk_traffic, axis=1)
    # _交通 は後方互換のため _交通1 と同じ内容を保持
    if "_交通1" in df.columns:
        df["_交通"] = df["_交通1"]

    # 間取りを結合
    if "間取り_部屋数" in df.columns:
        df["_間取り"] = (
            df["間取り_部屋数"].fillna("").astype(str) +
            df.get("間取り_種別", pd.Series("", index=df.index)).fillna("").astype(str)
        )

    # 物件種別の正規化
    if "種別" in df.columns:
        df["_物件種別"] = df["種別"].apply(lambda t: _TYPE_MAP.get(str(t).strip(), str(t).strip()))

    # 土地物件の建物名から「土地」サフィックスを除去
    if "物件名" in df.columns:
        has_type = "_物件種別" in df.columns
        def _clean_name(r):
            name = str(r.get("物件名", "") or "")
            if has_type and str(r.get("_物件種別", "")) in ("売地", "土地"):
                name = re.sub(r"[\s　]*(土地|売地|宅地)$", "", name).strip()
            return name
        df["_建物名_clean"] = df.apply(_clean_name, axis=1)

    return df


# ----------------------------------------------------------------
# DBインデックス構築（高速検索用）
# ----------------------------------------------------------------

def build_db_index(db_records: list[dict]) -> dict[tuple, list[dict]]:
    """
    (type_group, addr_key) → [db_rows] のインデックスを構築。
    O(1) で候補を絞り込むために使う。
    """
    idx: dict[tuple, list[dict]] = defaultdict(list)
    for row in db_records:
        tg = type_group(row.get("物件種別", ""))
        ak = addr_key(row.get("所在地", ""))
        idx[(tg, ak)].append(row)
    return idx


# ----------------------------------------------------------------
# マッチングロジック（種別ごとのルール）
# ----------------------------------------------------------------

def _addr_match(csv_addr: str, db_addr: str) -> bool:
    """住所（丁目レベル）の一致判定。"""
    n_csv = normalize(csv_addr)
    n_db  = normalize(db_addr)
    return bool(n_csv.startswith(n_db) or n_db in n_csv)


def _match_mansion(csv_row, candidates, cv, csv_addr, csv_stations, csv_company):
    """
    マンション照合:
      住所 + 建物名 + 所在階 が一致なら同一住戸。
      同一住戸を別会社が掲載している場合は CSV会社名と一致する候補を優先。
    """
    csv_name  = cv("建物名")
    csv_floor = cv("所在階")
    mansion_fallback = None

    for db_row in candidates:
        if not _addr_match(csv_addr, db_row.get("所在地", "")):
            continue

        db_name  = db_row.get("建物名", "")
        db_floor = db_row.get("所在階", "")
        if csv_name and db_name and not names_match(csv_name, db_name):
            continue
        if csv_floor and db_floor and not floor_eq(csv_floor, db_floor):
            continue

        if csv_name and db_name and csv_floor and db_floor:
            # 同一住戸候補
            db_company = db_row.get("会社名", "")
            if csv_company and db_company:
                if company_match(csv_company, db_company):
                    return db_row  # 会社名一致 → 最良マッチ
                if mansion_fallback is None:
                    mansion_fallback = db_row
                continue
            return db_row  # 会社名データなし → そのまま確定
        # データ欠損 → 駅+会社名で補完
        db_sensen = db_row.get("沿線駅", "")
        db_kotsuu = db_row.get("交通", "")
        if db_sensen and csv_stations and not station_match(csv_stations, db_sensen, db_kotsuu):
            continue
        db_company = db_row.get("会社名", "")
        if csv_company and db_company and not company_match(csv_company, db_company):
            continue
        return db_row

    return mansion_fallback


def _match_kodate_tochi(csv_row, candidates, cv, csv_addr, csv_stations, csv_company, tg):
    """
    戸建・土地照合（4段階フィルタ）:
      ① 必須マッチ: 住所(丁目) + 駅(両方データあり時) + 会社名(専任時のみ)
      ② 土地面積一致でフィルタ
      ③ 戸建のみ建物面積一致でさらに絞る
      ④ それでも複数なら最安価格を採用

    多棟現場で号棟ごとの面積が同じケースに対応するため、
    1件目で打ち切らず候補を集めてから絞り込む。
    """
    # ① 必須マッチ
    primary = []
    for db_row in candidates:
        if not _addr_match(csv_addr, db_row.get("所在地", "")):
            continue
        db_sensen = db_row.get("沿線駅", "")
        db_kotsuu = db_row.get("交通", "")
        if db_sensen and csv_stations and not station_match(csv_stations, db_sensen, db_kotsuu):
            continue
        # 取引態様: 一般媒介は会社名不問、それ以外は会社名一致を要求
        is_ippan = "一般" in str(db_row.get("取引態様", ""))
        if not is_ippan:
            db_company = db_row.get("会社名", "")
            if csv_company and db_company and not company_match(csv_company, db_company):
                continue
        primary.append(db_row)

    if not primary:
        return None

    # ② 土地面積一致でフィルタ
    csv_land = cv("土地面積")
    land_matched = [d for d in primary
                    if land_area_strict_eq(csv_land, d.get("土地面積", ""))]
    if not land_matched:
        return None
    if len(land_matched) == 1:
        return land_matched[0]

    # ③ 戸建のみ: 建物面積一致でさらに絞る
    if tg == "kodate":
        csv_bldg = cv("建物面積")
        bldg_matched = [d for d in land_matched
                        if land_area_strict_eq(csv_bldg, d.get("建物面積", ""))]
        if len(bldg_matched) == 1:
            return bldg_matched[0]
        if bldg_matched:
            land_matched = bldg_matched

    # ④ 最安価格を採用（同一現場の中で安い棟＝CSV側で出している棟）
    def _price_val(d):
        p = parse_price(d.get("価格", ""))
        return p if p is not None else float("inf")
    return min(land_matched, key=_price_val)


def _match_other(csv_row, candidates, cv, csv_addr, csv_stations, csv_company):
    """その他種別: 住所 + 駅 + 会社名 のシンプルマッチ（既存ロジック準拠）。"""
    for db_row in candidates:
        if not _addr_match(csv_addr, db_row.get("所在地", "")):
            continue
        db_sensen = db_row.get("沿線駅", "")
        db_kotsuu = db_row.get("交通", "")
        if db_sensen and csv_stations and not station_match(csv_stations, db_sensen, db_kotsuu):
            continue
        db_company = db_row.get("会社名", "")
        if csv_company and db_company and not company_match(csv_company, db_company):
            continue
        return db_row
    return None


def _match_in_candidates(
    csv_row: dict, candidates: list[dict], tg: str, cmap: dict[str, str]
) -> dict | None:
    """
    候補リスト内でマッチを探す（種別ごとに専用ロジックに振り分け）。

      マンション → _match_mansion: 住所+建物名+階数+(会社名タイブレーカー)
      戸建・土地 → _match_kodate_tochi: 4段階フィルタ（住所/駅/会社名→土地→建物→最安）
      その他    → _match_other: 住所+駅+会社名
    """
    def cv(field: str) -> str:
        col = cmap.get(field)
        return str(csv_row.get(col, "") or "") if col else ""

    csv_addr     = cv("所在地")
    csv_stations = _build_csv_stations(csv_row)
    csv_company  = cv("会社名")

    if tg == "mansion":
        return _match_mansion(csv_row, candidates, cv, csv_addr, csv_stations, csv_company)
    if tg in ("kodate", "tochi"):
        return _match_kodate_tochi(csv_row, candidates, cv, csv_addr, csv_stations, csv_company, tg)
    return _match_other(csv_row, candidates, cv, csv_addr, csv_stations, csv_company)


# ----------------------------------------------------------------
# 設定読み込み
# ----------------------------------------------------------------

def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"config.json が見つかりません: {CONFIG_PATH}")
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_column_map() -> dict:
    if not COL_MAP_PATH.exists():
        return {}
    raw = json.loads(COL_MAP_PATH.read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_") and v}


def detect_col_map(csv_cols: list[str], custom: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    lower_map = {c.lower().replace(" ", "").replace("　", ""): c for c in csv_cols}
    for field, aliases in FIELD_ALIASES.items():
        if field in custom and custom[field] in csv_cols:
            result[field] = custom[field]
            continue
        for alias in aliases:
            key = alias.lower().replace(" ", "").replace("　", "")
            if key in lower_map:
                result[field] = lower_map[key]
                break
    return result


# ----------------------------------------------------------------
# 照合メイン
# ----------------------------------------------------------------

def compare(csv_path: str, cfg: dict) -> dict:
    """
    CSVとREINS物件DBを照合して結果辞書を返す。

    アルゴリズム:
      1. DBを (type_group, addr_key) でインデックス化 → O(n)
      2. 各CSV行を種別+住所でDB候補に絞り込み → O(1) lookup
      3. 候補内でルールベースマッチ（建物名/面積/所在階など） → O(k), k≪DB件数
    """
    db_path    = cfg["db_path"]
    price_tol  = cfg.get("matching", {}).get("price_diff_tolerance_man", 1)
    # 成約確定の対象にする「成約・取消日」の新しさ（実行日から何日以内か）。
    # 週1回の作業のため、古い成約記録は誤判定の元になるので既定10日。
    sold_within_days = cfg.get("matching", {}).get("confirmed_sold_within_days", 10)
    custom_map = load_column_map()

    # ── CSV読み込み ───────────────────────────────────────────
    csv_df = None
    for enc in ("utf-8-sig", "cp932", "utf-8"):
        try:
            csv_df = pd.read_csv(csv_path, encoding=enc, dtype=str).fillna("")
            logger.info(f"CSV読み込み({enc}): {len(csv_df)}行")
            break
        except (UnicodeDecodeError, Exception):
            continue
    if csv_df is None or csv_df.empty:
        raise ValueError(f"CSVを読み込めません: {csv_path}")

    csv_df   = preprocess_csv(csv_df)
    csv_cols = list(csv_df.columns)
    cmap     = detect_col_map(csv_cols, custom_map)
    logger.info(f"列マッピング: {cmap}")

    unmapped = [f for f in ["建物名", "所在地", "価格"] if f not in cmap]
    if unmapped:
        logger.warning(f"マッピング未検出の重要フィールド: {unmapped}")

    # ── DB読み込み + インデックス構築 ─────────────────────────
    xl = pd.ExcelFile(db_path)
    db_df = pd.read_excel(xl, sheet_name="物件DB", dtype=str).fillna("")
    db_records = db_df.to_dict("records")
    db_index   = build_db_index(db_records)

    archive_records: list[dict] = []
    archive_index: dict[tuple, list[dict]] = defaultdict(list)
    if "成約・取消" in xl.sheet_names:
        arch_df = pd.read_excel(xl, sheet_name="成約・取消", dtype=str).fillna("")
        archive_records = arch_df.to_dict("records")
        for row in archive_records:
            tg = type_group(row.get("物件種別", ""))
            ak = addr_key(row.get("所在地", ""))
            archive_index[(tg, ak)].append(row)

    logger.info(f"DB: 物件DB={len(db_records)}件 / 成約・取消={len(archive_records)}件")

    # ── 照合 ──────────────────────────────────────────────────
    matched, price_changed, not_in_db, confirmed_sold = [], [], [], []

    def cv(row, field):
        col = cmap.get(field)
        return str(row.get(col, "") or "") if col else ""

    for _, row in csv_df.iterrows():
        csv_row = row.to_dict()
        tg  = type_group(cv(csv_row, "物件種別"))
        ak  = addr_key(cv(csv_row, "所在地"))

        candidates = db_index.get((tg, ak), [])
        db_match   = _match_in_candidates(csv_row, candidates, tg, cmap)

        # CSV由来の共通情報（物件管理番号・最寄駅）。WEB自動更新と通知の両方で使う
        kanri_no = cv(csv_row, "物件管理番号")
        eki_info = _csv_station_summary(csv_row)

        def _compare_fields(db_row: dict) -> dict:
            """CSV側とDB側を並べた比較表示用フィールド（GUI確認画面で使用）。"""
            return {
                "csv_建物名":   cv(csv_row, "建物名"),
                "csv_所在地":   cv(csv_row, "所在地"),
                "csv_最寄駅":   eki_info,
                "csv_物件種別": cv(csv_row, "物件種別"),
                "csv_土地面積": cv(csv_row, "土地面積"),
                "csv_建物面積": cv(csv_row, "建物面積"),
                "csv_専有面積": cv(csv_row, "専有面積"),
                "db_建物名":    str(db_row.get("建物名", "") or ""),
                "db_所在地":    str(db_row.get("所在地", "") or ""),
                "db_最寄駅":    _db_station_summary(db_row),
                "db_会社名":    str(db_row.get("会社名", "") or ""),
                "db_物件種別":  str(db_row.get("物件種別", "") or ""),
                "db_土地面積":  str(db_row.get("土地面積", "") or ""),
                "db_建物面積":  str(db_row.get("建物面積", "") or ""),
                "db_専有面積":  str(db_row.get("専有面積", "") or ""),
            }

        def _not_in_db_entry():
            return {
                "物件管理番号": kanri_no,
                "建物名":   cv(csv_row, "建物名"),
                "所在地":   cv(csv_row, "所在地"),
                "価格":     cv(csv_row, "価格"),
                "会社名":   cv(csv_row, "会社名"),
                "物件種別": cv(csv_row, "物件種別"),
                "最寄駅":   eki_info,
                "所在階":   cv(csv_row, "所在階"),
                "土地面積": cv(csv_row, "土地面積"),
                "建物面積": cv(csv_row, "建物面積"),
                "専有面積": cv(csv_row, "専有面積"),
            }

        if db_match is not None:
            # DBの状態が「取消候補」→ 成約候補として扱う
            if db_match.get("状態") == "取消候補":
                not_in_db.append(_not_in_db_entry())
            else:
                # 価格変更チェック（両方を万円単位に正規化してから比較・保存）
                csv_p = parse_price(cv(csv_row, "価格"))
                db_p  = parse_price(db_match.get("価格", ""))
                # 会社名比較情報を付与（ゲートとしては使わず表示用のみ）
                base = {
                    **db_match,
                    "物件管理番号": kanri_no,
                    "最寄駅":     eki_info,
                    "action":     "price",
                    "csv_会社名": cv(csv_row, "会社名"),
                    **_compare_fields(db_match),
                }
                if csv_p and db_p and abs(csv_p - db_p) > price_tol:
                    price_changed.append({
                        **base,
                        "csv_価格": f"{csv_p:.0f}",
                        "db_価格":  f"{db_p:.0f}",
                    })
                else:
                    matched.append(base)
        else:
            # 成約・取消シートも確認
            arch_candidates = archive_index.get((tg, ak), [])
            arch_match = _match_in_candidates(csv_row, arch_candidates, tg, cmap)
            # 成約確定にするのは「成約・取消日が実行日からN日以内」のものだけ。
            # 古い成約記録（前回以前のクリーニング分）は成約候補に回して手動確認に。
            if arch_match is not None and within_recent_days(
                arch_match.get("成約・取消日", ""), sold_within_days
            ):
                confirmed_sold.append({
                    **_not_in_db_entry(),
                    "action":       "sold",
                    "成約・取消日": arch_match.get("成約・取消日", ""),
                    "csv_会社名":   cv(csv_row, "会社名"),
                    "csv_価格":     cv(csv_row, "価格"),
                    "db_価格":      str(arch_match.get("価格", "") or ""),
                    **_compare_fields(arch_match),
                })
            else:
                not_in_db.append(_not_in_db_entry())

    logger.info(
        f"照合結果: 一致={len(matched)} 価格変更={len(price_changed)} "
        f"成約候補={len(not_in_db)} 成約確定={len(confirmed_sold)}"
    )
    return {
        "matched":         matched,
        "price_changed":   price_changed,
        "not_in_db":       not_in_db,
        "confirmed_sold":  confirmed_sold,
        "total_csv":       len(csv_df),
        "total_db":        len(db_records),
        "unmapped_fields": unmapped,
        "csv_cols":        csv_cols,
        "cmap":            cmap,
    }


# ----------------------------------------------------------------
# HTMLレポート保存
# ----------------------------------------------------------------

# レポートを保持する日数（これより古いHTMLレポートは自動削除）
REPORT_KEEP_DAYS = 14


def cleanup_old_reports(keep_days: int = REPORT_KEEP_DAYS) -> int:
    """
    reports/ 内の古いHTMLレポートを自動削除する（更新日時が keep_days 日より古いもの）。
    削除した件数を返す。エラーは無視（クリーニング自体で処理を止めない）。
    """
    import time
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for f in REPORT_DIR.glob("report_*.html"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except Exception:
            pass
    if removed:
        logger.info(f"古いレポートを削除: {removed}件（{keep_days}日より前）")
    return removed


def save_report(body_html: str, csv_name: str) -> Path:
    ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"report_{ts}_{Path(csv_name).stem}.html"
    path.write_text(body_html, encoding="utf-8")
    logger.info(f"レポート保存: {path}")
    # 古いレポートを自動削除（溜まり続けるのを防ぐ）
    cleanup_old_reports()
    return path


# 要確認物件の印刷用HTMLで1ページあたり何件を表示するか（既定10件）
REVIEW_PRINT_PER_PAGE = 10


def _print_fmt_price(s) -> str:
    """価格文字列を「○○万円」に整形（印刷HTML用の軽量版）。"""
    n = re.sub(r"[^\d.]", "", str(s).replace(",", ""))
    if not n:
        return "-"
    try:
        v = float(n)
        if v >= 10_000_000:
            v /= 10_000
        return f"{v:,.0f}万円"
    except ValueError:
        return str(s)


def save_print_review(result: dict, csv_name: str,
                      per_page: int = REVIEW_PRINT_PER_PAGE) -> Path | None:
    """
    要確認物件（成約候補 = DBにもなく成約・取消にも無いCSV物件）を
    印刷用HTMLとして保存する。 1ページあたり per_page 件で改ページ。
    複数人で分担して目視確認するための紙印刷を想定。
    """
    items = result.get("not_in_db", []) or []
    if not items:
        return None

    total = len(items)
    n_pages = (total + per_page - 1) // per_page
    now = datetime.now()

    rows_html = []
    for i, p in enumerate(items, start=1):
        # 1〜per_page 件目は1ページ目、per_page+1〜2*per_page 件目は2ページ目…
        page_break_css = "page-break-before:always;" if (i > 1 and (i - 1) % per_page == 0) else ""
        page_no = (i - 1) // per_page + 1

        name    = p.get("建物名", "") or "(建物名なし)"
        addr    = p.get("所在地", "") or "-"
        eki     = p.get("最寄駅", "") or "-"
        company = p.get("会社名", "") or "-"
        kanri   = p.get("物件管理番号", "") or "-"
        kind    = p.get("物件種別", "") or "-"
        floor   = p.get("所在階", "")
        price   = p.get("価格", "")
        land    = p.get("土地面積", "")
        bldg    = p.get("建物面積", "")
        senyu   = p.get("専有面積", "")

        areas = []
        if str(land).strip():  areas.append(f"土地 {land}㎡")
        if str(bldg).strip():  areas.append(f"建物 {bldg}㎡")
        if str(senyu).strip(): areas.append(f"専有 {senyu}㎡")
        area_str = " ／ ".join(areas) if areas else "-"

        floor_str = f"　階数: {floor}" if str(floor).strip() else ""

        rows_html.append(f"""
<div class="item" style="{page_break_css}">
  <div class="r1">
    <span class="num">{i}/{total}</span>
    <span class="kanri">{kanri}</span>
    <span class="kind">{kind}{floor_str}</span>
    <span class="price">{_print_fmt_price(price)}</span>
    <span class="check">□ 確認済み</span>
  </div>
  <div class="r2"><b>{name}</b></div>
  <div class="r3"><span class="lbl">住所:</span>{addr}　<span class="lbl">最寄:</span>{eki}</div>
  <div class="r4"><span class="lbl">会社:</span>{company}　<span class="lbl">面積:</span>{area_str}</div>
</div>""")

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>要確認物件 {total}件 ({n_pages}ページ)</title>
<style>
  @page {{ size: A4 portrait; margin: 10mm; }}
  body {{
    font-family: "Meiryo","Hiragino Sans",sans-serif;
    color: #222; font-size: 10px; margin: 0; padding: 4px;
    line-height: 1.25;
  }}
  .header {{
    background:#1c4587; color:white; padding:4px 10px;
    font-size:12px; font-weight:bold; border-radius:3px;
    margin-bottom:4px;
  }}
  .summary {{ font-size:9px; color:#555; margin-bottom:4px; line-height:1.2; }}
  .item {{
    border: 1px solid #888; padding: 3px 8px; margin-bottom: 3px;
    page-break-inside: avoid; border-radius: 2px;
  }}
  .item .r1 {{
    display:flex; gap:8px; align-items:center;
    font-weight:bold; margin-bottom:1px;
    border-bottom: 1px solid #eee; padding-bottom: 1px;
    font-size: 10px;
  }}
  .item .num   {{ color: #1c4587; min-width: 50px; }}
  .item .kanri {{ color: #333; min-width: 85px; }}
  .item .kind  {{ color: #7a4f00; flex-grow: 1; }}
  .item .price {{ color: #cc0000; font-size: 11px; }}
  .item .check {{ color: #888; font-weight: normal; font-size: 9px; min-width: 70px; text-align: right; }}
  .item .r2, .item .r3, .item .r4 {{ margin: 0; font-size: 10px; }}
  .item .r2 {{ font-size: 11px; }}
  .lbl {{ color: #555; }}
  @media print {{
    .header {{ -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
    .item {{ page-break-inside: avoid; }}
  }}
</style>
</head>
<body>
<div class="header">要確認物件（成約候補） 全 {total} 件 / {n_pages} ページ（1ページ {per_page} 件区切り）</div>
<div class="summary">
  作成日時: {now:%Y-%m-%d %H:%M}　|　対象CSV: {Path(csv_name).name}<br>
  REINS DBにも成約・取消シートにも見つからなかった物件です。担当者で分担して目視確認してください。
</div>
{"".join(rows_html)}
</body>
</html>"""

    ts   = now.strftime("%Y%m%d_%H%M%S")
    path = REPORT_DIR / f"print_review_{ts}_{Path(csv_name).stem}.html"
    path.write_text(html, encoding="utf-8")
    logger.info(f"印刷用ページ保存: {path}（{n_pages}ページ / {total}件）")
    return path


# 最新の照合結果（WEB自動更新ツールが読み込む）の保存先
LAST_RESULT_PATH = BASE_DIR / "last_result.json"


def save_result_json(result: dict, csv_path: str) -> Path:
    """
    照合結果を JSON 保存。WEB自動更新ツール（web_updater.py）が読み込んで
    成約処理・価格変更の対象一覧として使う。
    重い内部データ（cmap/csv_cols）は除外して必要分のみ保存する。
    """
    payload = {
        "csv_path":       csv_path,
        "generated_at":   datetime.now().isoformat(timespec="seconds"),
        "confirmed_sold": result.get("confirmed_sold", []),
        "price_changed":  result.get("price_changed", []),
        "not_in_db":      result.get("not_in_db", []),
        "total_csv":      result.get("total_csv", 0),
    }
    LAST_RESULT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"照合結果JSON保存: {LAST_RESULT_PATH}")
    return LAST_RESULT_PATH


# ----------------------------------------------------------------
# エントリーポイント
# ----------------------------------------------------------------

def main() -> None:
    root = tk.Tk()
    root.withdraw()

    # コマンドライン引数でCSVパスが渡された場合はそれを使用（ドラッグ＆ドロップ対応）
    if len(sys.argv) > 1:
        csv_path = sys.argv[1]
    else:
        downloads = Path.home() / "Downloads"
        csv_path = filedialog.askopenfilename(
            title="照合するCSVファイルを選択してください",
            initialdir=str(downloads),
            filetypes=[("CSVファイル", "*.csv"), ("すべてのファイル", "*.*")],
        )
        if not csv_path:
            messagebox.showinfo("キャンセル", "ファイルが選択されませんでした。")
            return

    logger.info(f"選択CSV: {csv_path}")

    try:
        cfg = load_config()
    except FileNotFoundError as e:
        messagebox.showerror("設定エラー", str(e))
        return

    try:
        result = compare(csv_path, cfg)
    except Exception as e:
        logger.exception("照合中にエラーが発生しました")
        messagebox.showerror("エラー", f"照合中にエラーが発生しました:\n{e}")
        return

    # WEB自動更新ツール用に結果をJSON保存
    try:
        save_result_json(result, csv_path)
    except Exception:
        logger.exception("照合結果JSONの保存に失敗しました（処理は継続）")

    # 要確認物件（成約候補）の印刷用HTMLを保存（10件/ページ、担当分担用）
    try:
        per_page = cfg.get("matching", {}).get("review_print_per_page", REVIEW_PRINT_PER_PAGE)
        save_print_review(result, csv_path, per_page=per_page)
    except Exception:
        logger.exception("印刷用ページの保存に失敗しました（処理は継続）")

    # 自動印刷（既定OFF。configで auto_print_review_after_match=true にすると有効）
    if cfg.get("auto_print_review_after_match", False):
        try:
            import subprocess
            subprocess.Popen(
                [sys.executable, str(BASE_DIR / "print_review.py")],
                cwd=str(BASE_DIR),
            )
            logger.info("自動印刷を起動しました（print_review.py）")
        except Exception:
            logger.exception("自動印刷の起動に失敗（処理は継続）")

    if result["unmapped_fields"]:
        msg = (
            f"以下の重要フィールドがCSVで検出できませんでした:\n"
            f"  {', '.join(result['unmapped_fields'])}\n\n"
            f"CSVの列名一覧:\n  {', '.join(result['csv_cols'][:20])}\n\n"
            f"column_map.json を編集して列名を指定してください。"
        )
        messagebox.showwarning("列マッピング警告", msg)

    csv_name    = Path(csv_path).name
    subject, body = build_check_email(result, csv_name)
    report_path   = save_report(body, csv_name)

    n_miss  = len(result["not_in_db"])
    n_price = len(result["price_changed"])
    n_conf  = len(result["confirmed_sold"])

    if n_miss + n_price + n_conf == 0:
        answer = messagebox.askyesno(
            "照合完了（変化なし）",
            f"CSV {result['total_csv']}件を照合しました。\n変化のある物件はありませんでした。\n\nメールを送信しますか？"
        )
        if not answer:
            messagebox.showinfo("完了", f"レポートを保存しました:\n{report_path}")
            return

    sent = send_email(cfg, subject, body)
    messagebox.showinfo(
        "照合完了",
        f"CSV件数:   {result['total_csv']}件\n"
        f"一致確認:  {len(result['matched'])}件\n"
        f"成約確定:  {n_conf}件\n"
        f"成約候補:  {n_miss}件\n"
        f"価格変更:  {n_price}件\n\n"
        f"レポート: {report_path}\n"
        f"メール: {'送信成功' if sent else '送信失敗（checker.log確認）'}"
    )


if __name__ == "__main__":
    main()
