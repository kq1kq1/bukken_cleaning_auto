"""REINS物件DBの読み込み時チェックのテスト。

reins_auto 側の変更で照合が黙って外れないよう、列や状態がおかしければ止まることを確かめる。
"""

import unittest
from datetime import datetime

import check_csv as c


def _record(**over) -> dict:
    rec = {col: "" for col in c.REQUIRED_DB_COLUMNS}
    rec["状態"] = "アクティブ"
    rec.update(over)
    return rec


def _db(records=None, archive=None) -> c.ReinsDb:
    return c.ReinsDb(
        [_record()] if records is None else records,
        [_record(**{"成約・取消日": "2026-10-01"})] if archive is None else archive,
        datetime.now(),
    )


class DbCheckTest(unittest.TestCase):

    def test_ok(self):
        """必要な列と状態がそろっていれば通る"""
        c.validate_reins_db(_db(records=[_record(), _record(状態="取消候補")]))

    def test_missing_column(self):
        """照合に使う列が無ければ止まる"""
        rec = _record()
        del rec["交通"]
        with self.assertRaisesRegex(ValueError, "交通"):
            c.validate_reins_db(_db(records=[rec]))

    def test_missing_closed_date(self):
        """成約・取消に「成約・取消日」が無ければ止まる"""
        with self.assertRaisesRegex(ValueError, "成約・取消日"):
            c.validate_reins_db(_db(archive=[_record()]))

    def test_unknown_status(self):
        """想定外の「状態」があれば止まる"""
        with self.assertRaisesRegex(ValueError, "保留"):
            c.validate_reins_db(_db(records=[_record(), _record(状態="保留")]))

    def test_empty(self):
        """物件DBが空なら止まる"""
        with self.assertRaisesRegex(ValueError, "空"):
            c.validate_reins_db(_db(records=[]))


if __name__ == "__main__":
    unittest.main()
