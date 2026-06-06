"""
手動の価格修正ツール（スカイヤーズ / ピタクラ 両サイト）

GUI画面で「物件管理番号と新価格(万円)」を1行1物件で入力すると、
両サイトを同じ価格に修正する。

入力例（区切りはスペース／カンマ／タブ どれでもOK）:
  HF403652 5180
  HF403684, 4980
  HF403545	2499

照合（check_csv.py）を経由せず、入力値をそのまま更新するため、
自動マッチング結果が間違っていた物件を**確実に正しい価格**へ戻せる。
"""

import re
import sys
import json
import threading
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import web_updater as wu
from mailer import send_email, build_update_result_email

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"


def parse_input(text: str) -> list[tuple[str, str]]:
    """
    各行をパース → (管理番号, 価格万円) のリスト。
    区切りはスペース／タブ／カンマを許容。価格は数字以外を除去。
    HFが付いていない番号でも先頭にHFを補完。
    """
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[,\t\s]+", line, maxsplit=1)
        if len(parts) < 2:
            continue
        no_raw = parts[0].strip()
        # HFが無ければ補完（数字だけ入力されてもOKに）
        if no_raw and not re.match(r"^[A-Za-z]", no_raw):
            no = "HF" + re.sub(r"[^\d]", "", no_raw)
        else:
            no = no_raw.upper()
        price = re.sub(r"[^\d]", "", parts[1])
        if no and price:
            out.append((no, price))
    return out


class FixApp:
    def __init__(self, root: tk.Tk, cfg: dict):
        self.root = root
        self.cfg = cfg
        self.running = False
        self._build_ui()

    def _build_ui(self):
        self.root.title("価格修正（両サイト同時）")
        self.root.geometry("760x560")

        hdr = tk.Frame(self.root, padx=10, pady=8)
        hdr.pack(fill="x")
        tk.Label(
            hdr,
            text="物件管理番号と新価格（万円）を1行1物件で入力",
            font=("Meiryo", 12, "bold"),
        ).pack(anchor="w")
        tk.Label(
            hdr,
            text="区切りはスペース／カンマ／タブどれでもOK。例: HF403652 5180",
            font=("Meiryo", 10), fg="#555",
        ).pack(anchor="w")

        self.text = tk.Text(self.root, font=("Consolas", 12), wrap="none", height=18)
        self.text.pack(fill="both", expand=True, padx=10, pady=4)
        # サンプル
        self.text.insert("1.0",
            "# 例:\n"
            "# HF403652 5180\n"
            "# HF403684 4980\n"
            "\n"
        )

        bottom = tk.Frame(self.root, padx=10, pady=8)
        bottom.pack(fill="x")
        self.status = tk.Label(bottom, text="準備完了", anchor="w", fg="#333")
        self.status.pack(side="left", fill="x", expand=True)

        self.btn_exec = tk.Button(
            bottom, text="入力した内容で両サイトを更新", bg="#cc0000", fg="white",
            font=("Meiryo", 10, "bold"), command=lambda: self.confirm(False),
        )
        self.btn_exec.pack(side="right", padx=4)
        self.btn_dry = tk.Button(
            bottom, text="ドライラン", command=lambda: self.confirm(True),
        )
        self.btn_dry.pack(side="right", padx=4)

    def confirm(self, dry_run: bool):
        if self.running:
            return
        items = parse_input(self.text.get("1.0", "end"))
        if not items:
            messagebox.showinfo("入力なし", "管理番号と価格の行が見つかりません。")
            return
        mode = "ドライラン（実更新なし）" if dry_run else "★本番実行★ 両サイトを実際に更新します"
        preview = "\n".join(f"  {k} → {p}万円" for k, p in items[:30])
        more = f"\n  ... 他 {len(items)-30}件" if len(items) > 30 else ""
        if not messagebox.askyesno(
            "実行確認",
            f"{mode}\n\n対象 {len(items)}件:\n{preview}{more}\n\n実行しますか？",
        ):
            return
        self._run_async(items, dry_run)

    def _run_async(self, items: list[tuple[str, str]], dry_run: bool):
        self.running = True
        self.btn_exec.config(state="disabled")
        self.btn_dry.config(state="disabled")
        self.status.config(
            text="実行中… ブラウザ画面が出ます。閉じないでください",
            fg="#1c4587",
        )

        # 入力からタスク構築（情報は最小限。check_csv経由ではなく直接）
        tasks = [
            wu.Task(
                kanri_no=k,
                action="price",
                new_price=p,
                info={
                    "csv_建物名":  "(手動修正)",
                    "csv_所在地":  "",
                    "csv_最寄駅":  "",
                    "csv_会社名":  "",
                    "csv_価格":    "",  # 現サイト価格は不明
                    "db_価格":     p,    # 目標値 = 入力値
                },
            )
            for k, p in items
        ]

        def worker():
            web = self.cfg.get("web_update", {})
            try:
                for site_key in ("skyhrs", "pitat"):
                    if site_key in web:
                        wu.run_site(site_key, web[site_key], tasks, dry_run, headless=False)
                if not dry_run:
                    rows = [
                        {"kanri_no": t.kanri_no, "action": t.action,
                         "new_price": t.new_price, "info": t.info, "results": t.results}
                        for t in tasks
                    ]
                    subject, body = build_update_result_email(rows)
                    send_email(self.cfg, "[手動修正] " + subject, body)
                self.root.after(0, lambda: self._done(tasks, dry_run))
            except Exception as e:
                self.root.after(0, lambda: self._error(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, tasks, dry_run: bool):
        self.running = False
        self.btn_exec.config(state="normal")
        self.btn_dry.config(state="normal")
        ok = sum(1 for t in tasks if t.results and all(v == "成功" for v in t.results.values()))
        ng = sum(1 for t in tasks if any(str(v).startswith("失敗") for v in t.results.values()))
        sk = len(tasks) - ok - ng
        self.status.config(text=f"完了: 成功{ok} / 失敗{ng} / スキップ{sk}", fg="#0a5d00")
        lines = []
        for t in tasks:
            lines.append(
                f"[{t.kanri_no}] {t.new_price}万円\n"
                f"   スカイヤーズ: {t.results.get('skyhrs','-')}\n"
                f"   ピタクラ:    {t.results.get('pitat','-')}"
            )
        mail_note = "" if dry_run else "\n\n結果メールを送信しました。"
        messagebox.showinfo(
            "ドライラン完了" if dry_run else "実行完了",
            f"成功{ok} / 失敗{ng} / スキップ{sk}{mail_note}\n\n" + "\n\n".join(lines[:30]),
        )

    def _error(self, msg: str):
        self.running = False
        self.btn_exec.config(state="normal")
        self.btn_dry.config(state="normal")
        self.status.config(text=f"エラー: {msg}", fg="#cc0000")
        messagebox.showerror("エラー", msg)


def main():
    if not CONFIG_PATH.exists():
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("エラー", f"{CONFIG_PATH} がありません。")
        return
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    root = tk.Tk()
    FixApp(root, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
