"""
WEB自動更新の初回ログイン・セッション保存ツール

2FAがないため、初回だけ手動でログインしてセッション（Cookie等）を保存し、
以降の web_updater.py はそのセッションを再利用する。

使い方:
  python login_setup.py            # 両サイトのログインをセットアップ
  python login_setup.py skyhrs     # スカイヤーズだけ
  python login_setup.py pitat      # ピタクラだけ

各サイトでブラウザが開くので、手動でログインして目的のページまで進めたら、
このコンソールで Enter を押すとセッションが保存される。
"""

import sys
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def setup_site(site_key: str, site_cfg: dict) -> None:
    """指定サイトでブラウザを開き、手動ログイン後にセッションを保存する。"""
    login_url  = site_cfg["login_url"]
    auth_state = BASE_DIR / site_cfg["auth_state"]

    print(f"\n===== {site_key} のログインセットアップ =====")
    print(f"ログインページを開きます: {login_url}")

    with sync_playwright() as p:
        # headless=False で実際の画面を表示（手動ログインのため）
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(login_url)

        print("\nブラウザでログインしてください。")
        print("ログインが完了して物件検索ができる状態まで進めたら、")
        input("このウィンドウで Enter を押してください... ")

        # セッション（Cookie + localStorage）を保存
        context.storage_state(path=str(auth_state))
        print(f"セッションを保存しました: {auth_state}")
        browser.close()


def main() -> None:
    cfg = load_config()
    web = cfg.get("web_update", {})

    targets = sys.argv[1:] if len(sys.argv) > 1 else ["skyhrs", "pitat"]
    for key in targets:
        if key not in web:
            print(f"⚠️ '{key}' は config.json の web_update にありません。スキップします。")
            continue
        setup_site(key, web[key])

    print("\n完了しました。web_updater.py が使えるようになりました。")


if __name__ == "__main__":
    main()
