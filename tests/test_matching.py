"""照合事例テスト。

事例フォルダの 自社CSV.csv（自社の物件一覧CSVの行）と REINS.csv（REINS物件DBの行）を
事例IDで組にして1件ずつ照合し、「期待結果」どおりに判定されるかを確かめる。

  tests/cases/        架空の事例（GitHubに上がる）
  tests/cases_local/  実際の物件で作った事例（.gitignore で除外。リポジトリが公開のため）

事例の足し方は tests/README.md を参照。
"""

import csv
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import check_csv
import web_updater

TESTS_DIR = Path(__file__).parent
CASE_DIRS = [TESTS_DIR / "cases", TESTS_DIR / "cases_local"]

# 照合結果のキー → 事例表の「期待結果」に書く言葉
RESULT_NAMES = {
    "matched":        "一致",
    "price_changed":  "価格変更",
    "confirmed_sold": "成約確定",
    "not_in_db":      "成約候補",
}
# 事例表だけで使う列（照合には渡さない）
CSV_META = ("事例ID", "期待結果", "期待する物件番号", "メモ")
DB_META = ("事例ID", "区分")

# config.example.json の既定値と同じ
CFG = {"matching": {"price_diff_tolerance_man": 1, "confirmed_sold_within_days": 10}}


def _read_rows(path: Path) -> list[dict]:
    """CSVを読む（Excelで保存したBOM付きにも対応）。空行は飛ばす。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [
            {k: (v or "").strip() for k, v in row.items() if k}
            for row in csv.DictReader(f)
            if any((v or "").strip() for v in row.values())
        ]


def _date(s: str) -> str:
    """「3日前」と書いた日付を実行日基準の YYYY-MM-DD にする。
    日付を直接書くと、日が経つと「直近の成約」でなくなり結果が変わるため。"""
    m = re.fullmatch(r"(\d+)日前", s)
    if m:
        return (datetime.now() - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d")
    return s


def load_cases(case_dir: Path) -> list[dict]:
    """事例フォルダを読み、事例ごとの dict のリストを返す。"""
    db_rows: dict[str, dict[str, list]] = {}
    for row in _read_rows(case_dir / "REINS.csv"):
        kind = "archive" if row["区分"] == "成約・取消" else "records"
        rec = {k: v for k, v in row.items() if k not in DB_META}
        rec["成約・取消日"] = _date(rec.get("成約・取消日", ""))
        if kind == "records":
            rec["状態"] = rec.get("状態") or "アクティブ"
        db_rows.setdefault(row["事例ID"], {"records": [], "archive": []})[kind].append(rec)

    cases = []
    for row in _read_rows(case_dir / "自社CSV.csv"):
        cid = row["事例ID"]
        csv_row = {k: v for k, v in row.items() if k not in CSV_META}
        csv_row["物件管理番号"] = cid
        db = db_rows.get(cid, {"records": [], "archive": []})
        cases.append({
            "id":          f"{case_dir.name}/{cid}",
            "memo":        row.get("メモ", ""),
            "expected":    row["期待結果"],
            "expected_no": row.get("期待する物件番号", ""),
            "csv_row":     csv_row,
            "db":          check_csv.ReinsDb(db["records"], db["archive"], datetime.now()),
        })
    return cases


def run_case(case: dict) -> dict:
    """1事例を照合する。DBの読み込みだけ事例のデータに差し替える。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "自社CSV.csv"
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(case["csv_row"]))
            w.writeheader()
            w.writerow(case["csv_row"])
        with mock.patch.object(check_csv, "load_reins_db", return_value=case["db"]):
            return check_csv.compare(str(path), CFG)


class MatchingCaseTest(unittest.TestCase):

    def test_cases(self):
        """事例表のとおりに判定される"""
        cases = [c for d in CASE_DIRS if (d / "自社CSV.csv").exists() for c in load_cases(d)]
        self.assertTrue(cases, "事例がありません")

        for case in cases:
            with self.subTest(case=case["id"], memo=case["memo"]):
                self.assertIn(case["expected"], RESULT_NAMES.values(),
                              f"期待結果は {'/'.join(RESULT_NAMES.values())} のどれかで書いてください")
                result = run_case(case)
                got = [name for key, name in RESULT_NAMES.items() if result[key]]
                self.assertEqual(got, [case["expected"]], "判定が期待と違います")

                hits = result["matched"] + result["price_changed"]
                if case["expected_no"]:
                    self.assertEqual(hits[0].get("物件番号"), case["expected_no"],
                                     "照合相手のREINS物件が期待と違います")

                # 価格変更は「REINSの新価格」をサイトに入れる（逆向きの致命バグが過去にあった）
                if result["price_changed"]:
                    [task] = web_updater.build_tasks(result)
                    reins_price = check_csv.parse_price(hits[0]["価格"])
                    self.assertEqual(task.new_price, f"{reins_price:.0f}",
                                     "サイトに入れる価格がREINSの価格になっていません")


if __name__ == "__main__":
    unittest.main()
