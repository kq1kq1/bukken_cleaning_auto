"""照合で使う判定部品（表記ゆれ・価格・面積・階・駅・会社名）のテスト。

部品の中身を書き換えても、ここに書いた「同じとみなす／別とみなす」が
変わっていなければ照合の結果は変わらない。
"""

import unittest
from datetime import datetime, timedelta

import check_csv as c


def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _stations(line: str, sta: str, walk: str = "", bus_walk: str = "", bus_ride: str = "") -> list:
    """自社CSVの交通手段1の列から、照合用の駅データを作る。"""
    row = {"交通手段1_沿線": line, "交通手段1_駅": sta}
    if bus_ride:
        row.update({"交通手段1_所要時間_区分": "バス",
                    "交通手段1_所要時間_バス停徒歩（分）": bus_walk,
                    "交通手段1_バス乗車（分）": bus_ride})
    else:
        row["交通手段1_所要時間_駅徒歩（分）"] = walk
    return c._build_csv_stations(row)


class RulesTest(unittest.TestCase):

    def check(self, func, cases):
        for *args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(func(*args), expected)

    def test_addr_key(self):
        """所在地は丁目（丁目が無ければ町名）までで同じ場所とみなす"""
        self.check(lambda a, b: c.addr_key(a) == c.addr_key(b), [
            ("東京都江戸川区西葛西３丁目", "東京都江戸川区西葛西3丁目5-10", True),
            ("千葉県松戸市中和倉", "千葉県松戸市中和倉123-4", True),
            ("東京都江戸川区西葛西３丁目", "東京都江戸川区西葛西4丁目5-10", False),
        ])

    def test_parse_price(self):
        """価格は万円にそろえる（円で書かれていても万円に直す）"""
        self.check(c.parse_price, [
            ("3480", 3480.0),
            ("3,480", 3480.0),
            ("３４８０万円", 3480.0),
            ("34800000", 3480.0),
            ("", None),
        ])

    def test_floor_eq(self):
        """所在階は「3」「3階」「3F」を同じとみなす"""
        self.check(c.floor_eq, [
            ("3", "3階", True),
            ("3F", "3階", True),
            ("３", "3階", True),
            ("3", "13階", False),
            ("", "3階", False),
        ])

    def test_land_area_strict_eq(self):
        """土地面積：整数部は必ず一致。小数は両方にあるときだけ一致が必要"""
        self.check(c.land_area_strict_eq, [
            ("100", "100", True),
            ("100", "100.5", True),
            ("100.5", "100.5", True),
            ("100.3", "100.5", False),
            ("100", "101", False),
        ])

    def test_names_match(self):
        """建物名は空白・記号・全角半角の違いや、片方がもう片方を含む場合は同じとみなす"""
        self.check(c.names_match, [
            ("テストパレス西葛西", "テストパレス　西葛西", True),
            ("ＴＥＳＴパレス", "TESTパレス", True),
            ("プレジャー・ガーデン", "プレジャーガーデン", True),
            ("テストパレス西葛西", "テストパレス西葛西Ａ棟", True),
            ("テストパレス西葛西", "サンプルハイツ行徳", False),
        ])

    def test_company_match(self):
        """会社名は法人格・営業所名・電話番号の違いを吸収する"""
        self.check(c.company_match, [
            ("株式会社テスト建設", "テスト建設(株)", True),
            ("テスト建設株式会社 船橋営業所 047-123-4567", "(株)テスト建設", True),
            ("一建設 柏", "一建設 松戸", True),
            ("テスト建設", "サンプル住宅", False),
            ("テスト建設", "", False),
        ])

    @unittest.expectedFailure
    def test_company_match_known_issue(self):
        """【既知の問題・要相談】似た名前の別会社を同じ会社とみなしてしまう

        カタカナ→ローマ字の救済（"living-net" と "リビング-ネット" を同じとみなす）や
        類似度0.75の判定が甘く、名前が少し似ているだけの別会社が一致する。
        マンションの「同じ部屋を複数社が掲載」の選び分けや、専任の戸建・土地で
        別会社の物件と照合してしまうおそれがある。
        判定ルールを直したら @unittest.expectedFailure を外すこと。
        """
        self.check(c.company_match, [
            ("サンプル不動産", "アップル不動産", False),
            ("タカラ不動産", "サクラ不動産", False),
            ("ミライ建設", "マルイ建設", False),
            ("アイホーム", "ケイホーム", False),
        ])

    def test_station_match(self):
        """駅：駅名＋徒歩分が一致。バス便はバス停徒歩＋乗車分の両方が一致"""
        self.check(c.station_match, [
            (_stations("東西線", "西葛西", walk="7"), "東西線 西葛西", "徒歩 7分", True),
            (_stations("東西線", "西葛西", walk="8"), "東西線 西葛西", "徒歩 7分", False),
            (_stations("総武線", "本八幡", walk="15"), "総武中央線 本八幡", "徒歩 15分", True),
            (_stations("都営新宿線", "本八幡", bus_walk="3", bus_ride="10"),
             "都営新宿線 本八幡", "停歩 3分/バス 10分", True),
            (_stations("都営新宿線", "本八幡", bus_walk="3", bus_ride="12"),
             "都営新宿線 本八幡", "停歩 3分/バス 10分", False),
            (_stations("都営新宿線", "本八幡", walk="10"),
             "都営新宿線 本八幡", "停歩 3分/バス 10分", False),
            (_stations("京成本線", "京成八幡", walk="5"), "京成本線 八幡", "徒歩 5分", True),
            (_stations("東西線", "西葛西", walk="7"), "東西線 西葛西", "", True),
            (_stations("東西線", "葛西", walk="7"), "東西線 西葛西", "徒歩 7分", False),
        ])

    def test_within_recent_days(self):
        """成約・取消日が10日以内かどうか（未来日は2日までのズレを許す）"""
        self.check(lambda s: c.within_recent_days(s, 10), [
            (_days_ago(3), True),
            (_days_ago(10), True),
            (_days_ago(11), False),
            (_days_ago(-1), True),
            (_days_ago(3).replace("-", "/"), True),
            ("", False),
        ])

    def test_type_group(self):
        """物件種別をマンション／戸建／土地に分ける"""
        self.check(c.type_group, [
            ("中古マンション", "mansion"),
            ("新築戸建", "kodate"),
            ("中古戸建", "kodate"),
            ("売地", "tochi"),
            ("33", "other"),
        ])


if __name__ == "__main__":
    unittest.main()
