"""
自動更新 確認＆実行GUI

check_csv.py が出力した last_result.json を読み込み、
成約確定・価格変更の一覧をチェックリストで表示する。
各物件は「CSVではこう / DBではこう」を並べて表示し、人の目で確認できる。

使い方:
  - 全部チェック済みの状態で開く（=実行対象）
  - 怪しい物件のチェックを外す（=除外）
  - 「ドライラン」で動作確認、「本番実行」で実際に更新
  - 実行後、結果を別メールで通知

  python review_and_update.py
"""

import re
import sys
import json
import threading
from pathlib import Path

import tkinter as tk
from tkinter import ttk, messagebox

import web_updater as wu
from mailer import send_email, build_update_result_email

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
RESULT_PATH = BASE_DIR / "last_result.json"


def fmt_price(s) -> str:
    """価格文字列を「○○万円」に整形（円単位は万円に換算）。"""
    n = re.sub(r"[^\d.]", "", str(s).replace(",", ""))
    if not n:
        return str(s) if s else "-"
    try:
        v = float(n)
        if v >= 10_000_000:   # 1億以上は円単位とみなして万円へ
            v /= 10_000
        return f"{v:.0f}万円"
    except ValueError:
        return str(s)


class ReviewApp:
    def __init__(self, root: tk.Tk, items: list[dict], cfg: dict):
        self.root = root
        self.cfg  = cfg
        self.items = items
        self.vars: list[tk.BooleanVar] = []
        self.running = False
        self._build_ui()

    # ---------------- UI構築 ----------------
    def _build_ui(self):
        self.root.title("自動更新 確認＆実行")
        self.root.geometry("980x680")

        n_sold  = sum(1 for it in self.items if it.get("action") == "sold")
        n_price = sum(1 for it in self.items if it.get("action") == "price")

        top = tk.Frame(self.root, padx=10, pady=8)
        top.pack(fill="x")
        tk.Label(
            top,
            text=f"対象 {len(self.items)}件（成約確定 {n_sold} / 価格変更 {n_price}）"
                 "　チェックを外すと除外されます",
            font=("Meiryo", 11, "bold"),
        ).pack(side="left")
        tk.Button(top, text="全解除", command=self.deselect_all).pack(side="right", padx=4)
        tk.Button(top, text="全選択", command=self.select_all).pack(side="right", padx=4)

        # スクロール可能なリスト領域
        mid = tk.Frame(self.root)
        mid.pack(fill="both", expand=True, padx=10)
        canvas = tk.Canvas(mid, highlightthickness=0)
        sb = ttk.Scrollbar(mid, orient="vertical", command=canvas.yview)
        self.list_frame = tk.Frame(canvas)
        self.list_frame.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.list_frame, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # マウスホイールでスクロール
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        for it in self.items:
            self._add_row(self.list_frame, it)

        # 下部: ステータス + 実行ボタン
        bottom = tk.Frame(self.root, padx=10, pady=8)
        bottom.pack(fill="x")
        self.status = tk.Label(bottom, text="準備完了", anchor="w", fg="#333")
        self.status.pack(side="left", fill="x", expand=True)
        self.btn_exec = tk.Button(
            bottom, text="チェックした物件を更新実行", bg="#1c4587", fg="white",
            font=("Meiryo", 10, "bold"), command=lambda: self.confirm_run(dry_run=False),
        )
        self.btn_exec.pack(side="right", padx=4)
        self.btn_dry = tk.Button(
            bottom, text="ドライラン(確認のみ)", command=lambda: self.confirm_run(dry_run=True),
        )
        self.btn_dry.pack(side="right", padx=4)

    def _add_row(self, parent, it: dict):
        var = tk.BooleanVar(value=True)
        self.vars.append(var)

        frame = tk.Frame(parent, bd=1, relief="solid", padx=6, pady=4)
        frame.pack(fill="x", pady=3)

        is_sold = it.get("action") == "sold"
        badge_txt = "成約" if is_sold else "価格変更"
        badge_bg  = "#cc0000" if is_sold else "#e6ac00"

        head = tk.Frame(frame)
        head.pack(fill="x")
        tk.Checkbutton(head, variable=var).pack(side="left")
        tk.Label(head, text=badge_txt, bg=badge_bg, fg="white",
                 font=("Meiryo", 9, "bold"), padx=6).pack(side="left", padx=(0, 6))
        tk.Label(head, text=it.get("物件管理番号", ""), font=("Meiryo", 10, "bold")).pack(side="left")

        # 価格表示
        if is_sold:
            price_txt = f"成約日: {it.get('成約・取消日', '')}"
        else:
            price_txt = f"{fmt_price(it.get('db_価格',''))} → {fmt_price(it.get('csv_価格',''))}"
        tk.Label(head, text=price_txt, fg="#7a4f00").pack(side="right")

        # CSV / DB 比較（2行）
        csv_line = (
            f"CSV: {it.get('csv_建物名','')}｜{it.get('csv_所在地','')}"
            f"｜{it.get('csv_最寄駅','')}｜{it.get('csv_会社名','')}"
        )
        db_line = (
            f"DB : {it.get('db_建物名','')}｜{it.get('db_所在地','')}"
            f"｜{it.get('db_最寄駅','')}｜{it.get('db_会社名','')}"
        )
        tk.Label(frame, text=csv_line, anchor="w", justify="left",
                 font=("Meiryo", 9), fg="#1c4587", wraplength=900).pack(fill="x")
        tk.Label(frame, text=db_line, anchor="w", justify="left",
                 font=("Meiryo", 9), fg="#0a5d00", wraplength=900).pack(fill="x")

    # ---------------- 操作 ----------------
    def select_all(self):
        for v in self.vars:
            v.set(True)

    def deselect_all(self):
        for v in self.vars:
            v.set(False)

    def _selected_items(self) -> list[dict]:
        return [it for it, v in zip(self.items, self.vars) if v.get()]

    def confirm_run(self, dry_run: bool):
        if self.running:
            return
        sel = self._selected_items()
        if not sel:
            messagebox.showinfo("確認", "チェックされた物件がありません。")
            return
        mode = "ドライラン（実際の更新はしません）" if dry_run else "★本番実行★（実際にサイトを更新します）"
        if not messagebox.askyesno("実行確認", f"{mode}\n\n対象: {len(sel)}件\n実行しますか？"):
            return
        self._run_async(sel, dry_run)

    def _run_async(self, sel: list[dict], dry_run: bool):
        self.running = True
        self.btn_exec.config(state="disabled")
        self.btn_dry.config(state="disabled")
        self.status.config(text="実行中… ブラウザ画面で進行します（閉じないでください）", fg="#1c4587")

        # 選択物件からタスクを構築
        tasks = [
            wu.Task(
                kanri_no=str(it.get("物件管理番号", "")).strip(),
                action=it.get("action", ""),
                new_price=str(it.get("csv_価格", "")).strip(),
                info=it,
            )
            for it in sel
        ]

        def worker():
            web = self.cfg.get("web_update", {})
            try:
                for site_key in ("skyhrs", "pitat"):
                    if site_key in web:
                        wu.run_site(site_key, web[site_key], tasks, dry_run, headless=False)
                # 本番のみ結果メール送信
                if not dry_run:
                    rows = [
                        {"kanri_no": t.kanri_no, "action": t.action,
                         "new_price": t.new_price, "info": t.info, "results": t.results}
                        for t in tasks
                    ]
                    subject, body = build_update_result_email(rows)
                    send_email(self.cfg, subject, body)
                self.root.after(0, lambda: self._done(tasks, dry_run))
            except Exception as e:
                self.root.after(0, lambda: self._error(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _done(self, tasks, dry_run: bool):
        self.running = False
        self.btn_exec.config(state="normal")
        self.btn_dry.config(state="normal")
        # 集計
        ok = sum(1 for t in tasks if t.results and all(v == "成功" for v in t.results.values()))
        ng = sum(1 for t in tasks if any(str(v).startswith("失敗") for v in t.results.values()))
        sk = len(tasks) - ok - ng
        self.status.config(text=f"完了: 成功{ok} / 失敗{ng} / スキップ{sk}", fg="#0a5d00")
        lines = []
        for t in tasks:
            name = t.info.get("csv_建物名") or t.info.get("建物名") or ""
            lines.append(f"[{t.kanri_no}] {name}\n   スカイヤーズ: {t.results.get('skyhrs','-')}\n   ピタクラ: {t.results.get('pitat','-')}")
        mail_note = "" if dry_run else "\n\n結果を自動更新メールで送信しました。"
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
    if not RESULT_PATH.exists():
        root = tk.Tk(); root.withdraw()
        messagebox.showerror("エラー", f"{RESULT_PATH.name} がありません。\n先に 実行.bat でCSV照合をしてください。")
        return
    result = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    cfg    = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    items = (result.get("confirmed_sold", []) or []) + (result.get("price_changed", []) or [])
    if not items:
        root = tk.Tk(); root.withdraw()
        messagebox.showinfo("確認", "成約確定・価格変更の対象がありません。")
        return

    root = tk.Tk()
    ReviewApp(root, items, cfg)
    root.mainloop()


if __name__ == "__main__":
    main()
