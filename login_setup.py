"""
WEB自動更新の初回ログイン・Chromeプロフィール作成ツール

実Chrome（システム既定）を Playwright で起動し、ユーザー専用の
プロフィールディレクトリで開く。そこで Google アカウントにログイン
してパスワードを Chrome に保存しておくと、以降のセッション切れ時に
Chrome の自動入力でログインが完結する。

  chrome_profile_skyhrs/   ← スカイヤーズ用プロフィール
  chrome_profile_pitat/    ← ピタクラ用プロフィール

使い方:
  python login_setup.py            # 両サイト
  python login_setup.py skyhrs     # スカイヤーズだけ
  python login_setup.py pitat      # ピタクラだけ

各サイトで Chrome が開くので：
  1. Google アカウントでChromeにサインイン
  2. 対象サイトでログイン → Chromeが「パスワードを保存」を提案するので保存
  3. このコンソールで Enter
これでセッション切れ時に Chrome の保存パスワードで自動再ログインできる。
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
    """指定サイトの専用Chromeプロフィールを作り、手動ログインしてもらう。"""
    login_url    = site_cfg["login_url"]
    profile_dir  = BASE_DIR / f"chrome_profile_{site_key}"
    profile_dir.mkdir(exist_ok=True)

    print(f"\n===== {site_key} のログイン設定 =====")
    print(f"プロフィール: {profile_dir}")
    print(f"ログインURL : {login_url}")

    with sync_playwright() as p:
        # 実Chromeを永続プロフィールで起動（パスワード保存が効く）
        try:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",          # システム既定の Chrome を使う
                headless=False,
                viewport={"width": 1280, "height": 800},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            print(f"❌ Chrome起動失敗: {e}")
            print("   Chromeがインストールされているか確認してください。")
            print("   未インストールの場合: https://www.google.com/chrome/")
            return

        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto(login_url, timeout=15000)
            opened = True
        except Exception as e:
            opened = False
            print(f"\n⚠️  URLの自動オープンに失敗: {e}")

        print("\n----- 手順 -----")
        if not opened:
            print(f"※ アドレスバーに貼り付けてください: {login_url}")
        print("1. Chrome右上のプロフィールアイコンからGoogleアカウントでログイン（推奨）")
        print("2. このサイトにIDとパスワードでログイン")
        print("3. Chromeが「パスワードを保存しますか？」と聞いたら【保存】")
        print("4. ログイン後の画面（物件検索など）まで進める")
        print("5. このコンソールで Enter")
        print("   ※ 時間制限はありません。")

        try:
            input("\n完了したら Enter… ")
        except (EOFError, KeyboardInterrupt):
            print("中断されました。")
            context.close()
            return

        print(f"✅ プロフィール保存: {profile_dir}")
        context.close()


def main() -> None:
    cfg = load_config()
    web = cfg.get("web_update", {})

    targets = sys.argv[1:] if len(sys.argv) > 1 else ["skyhrs", "pitat"]
    for key in targets:
        if key not in web:
            print(f"⚠️ '{key}' は config.json の web_update にありません。スキップします。")
            continue
        setup_site(key, web[key])

    print("\n完了しました。")
    print("以降はセッション切れ時にChrome保存パスワードで自動再ログインされます。")


if __name__ == "__main__":
    main()
