"""
WEB自動更新ツール（スカイヤーズ / ピタクラ）

check_csv.py が出力した last_result.json を読み込み、
  - 成約確定（confirmed_sold） → 成約処理（販売中止/成約）
  - 価格変更（price_changed）   → 価格を更新
を2つの自社サイトで自動実行する。

安全のためデフォルトはドライラン（実際の更新はしない）。

使い方:
  python web_updater.py                      # ドライラン（既定）。何をするか表示のみ
  python web_updater.py --execute            # 実際に更新を実行
  python web_updater.py --only HF403592      # 特定の物件番号だけ（複数可: HF1,HF2）
  python web_updater.py --limit 2            # 先頭2件だけ
  python web_updater.py --site skyhrs        # スカイヤーズだけ（既定: both）
  python web_updater.py --site pitat --only HF403592 --execute

  ※ 初回は login_setup.py でログインセッションを保存しておくこと。
"""

import sys
import re
import json
import time
import argparse
import logging
from pathlib import Path
from dataclasses import dataclass, field

# Windowsコンソールでの文字化け防止（標準出力をUTF-8に）
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE_DIR    = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
RESULT_PATH = BASE_DIR / "last_result.json"
LOG_PATH    = BASE_DIR / "web_updater.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ----------------------------------------------------------------
# タスク定義
# ----------------------------------------------------------------

@dataclass
class Task:
    """1物件に対する更新タスク。"""
    kanri_no:  str            # 物件管理番号（HF...）
    action:    str            # "sold"(成約) / "price"(価格変更)
    new_price: str            # 価格変更時の新価格（万円）。soldでは空
    info:      dict           # 表示用情報（建物名/所在地/会社名/最寄駅 等）
    results:   dict = field(default_factory=dict)  # {site: status文字列}


def build_tasks(result: dict) -> list[Task]:
    """照合結果JSONから更新タスク一覧を生成する。"""
    tasks: list[Task] = []
    for p in result.get("confirmed_sold", []):
        no = str(p.get("物件管理番号", "")).strip()
        if not no:
            continue
        tasks.append(Task(kanri_no=no, action="sold", new_price="", info=p))
    for p in result.get("price_changed", []):
        no = str(p.get("物件管理番号", "")).strip()
        if not no:
            continue
        tasks.append(Task(kanri_no=no, action="price",
                          new_price=str(p.get("csv_価格", "")).strip(), info=p))
    return tasks


# ----------------------------------------------------------------
# スカイヤーズ（https://sys.arcs.jp）
# ----------------------------------------------------------------

def process_skyhrs(context, site_cfg: dict, tasks: list[Task], dry_run: bool,
                   pause: bool = False) -> None:
    """スカイヤーズで各タスクを処理し、task.results['skyhrs'] に結果を記録する。"""
    search_url = site_cfg["search_url"]
    page = context.new_page()

    # 直前のダイアログ文言を捕捉（成功判定用）。accept失敗は無視（閉じ際の競合対策）
    dialog_log: list[str] = []

    def _on_dialog(d):
        dialog_log.append(d.message)
        try:
            d.accept()
        except Exception:
            pass

    page.on("dialog", _on_dialog)

    for task in tasks:
        key = "skyhrs"
        try:
            page.goto(search_url, wait_until="domcontentloaded")
            # セッション切れ判定（ログインページに飛ばされていないか）
            if "login" in page.url.lower():
                task.results[key] = "失敗:セッション切れ(要login_setup)"
                continue

            # 管理番号で検索（スカイヤーズは先頭"HF"を除いた数字列で登録されている）
            search_no = re.sub(r"(?i)^hf", "", task.kanri_no).strip()
            # クリック→入力→Tabでblur。手動操作と同じく change/blur イベントを発火させる
            memo = page.locator('input[name="MEMO"]')
            memo.click()
            memo.fill(search_no)
            memo.press("Tab")     # フォーカスを外して change/blur を発火（bt_search対策）
            time.sleep(0.3)

            # --pause: 検索ボタンを押す前に停止。ユーザーが手動で条件確認・検索する
            if pause:
                logger.info(f"\n  [一時停止] 管理番号欄に '{search_no}' を入力しました。")
                logger.info("  ブラウザで検索条件を確認し、手動で検索して挙動を確認してください。")
                input("  確認が終わったら、このウィンドウで Enter を押すと終了します... ")
                task.results[key] = "一時停止(手動確認)"
                break

            # 検索ボタンはフォーム送信（ページ遷移）なので遷移完了を待つ
            try:
                with page.expect_navigation(wait_until="load", timeout=20000):
                    page.click('input[name="srch"]')
            except PWTimeout:
                # 遷移しなかった場合（AJAX等）はネットワーク静定を待つ
                page.wait_for_load_state("networkidle")

            # 結果（公開フラグのプルダウン）が出るまで最大10秒待つ
            try:
                page.wait_for_selector('select[name^="KOUKAI_FLAG:"]', timeout=10000)
            except PWTimeout:
                pass  # 0件の可能性

            # 検索結果件数（公開フラグのプルダウン数で判定）
            selects = page.locator('select[name^="KOUKAI_FLAG:"]')
            n = selects.count()
            logger.info(f"  skyhrs {task.kanri_no}(検索値={search_no}): 結果{n}件 / URL={page.url}")
            if n == 0:
                # デバッグ: 結果ページの状態を保存して原因調査できるようにする
                try:
                    page.screenshot(path=str(BASE_DIR / "debug_skyhrs.png"), full_page=True)
                    (BASE_DIR / "debug_skyhrs.html").write_text(page.content(), encoding="utf-8")
                    logger.info("  → debug_skyhrs.png / debug_skyhrs.html を保存しました")
                except Exception:
                    pass
                task.results[key] = "スキップ:0件(該当なし)"
                continue
            if n > 1:
                task.results[key] = f"スキップ:複数ヒット({n}件)→手動確認"
                continue

            if dry_run:
                act = "成約に変更" if task.action == "sold" else f"価格→{task.new_price}万円"
                task.results[key] = f"DRY-RUN: {act}"
                continue

            # 実更新
            dialog_log.clear()
            if task.action == "sold":
                selects.first.select_option(value="3")  # 3=成約
            else:
                page.locator('input[name^="PRICE_FROM:"]').first.fill(task.new_price)

            dialog_before = len(dialog_log)
            page.click('input[name="bt_regist"]')  # 入力内容で更新

            # 確認→完了ダイアログが出るまで最大10秒ポーリング。
            # ※ time.sleep だと Playwright のイベントループが止まりダイアログを
            #   処理できないため、必ず page.wait_for_timeout でループを回す
            ok = False
            for _ in range(20):
                page.wait_for_timeout(500)  # イベントループを回しつつ0.5秒待つ
                if any("完了" in m for m in dialog_log[dialog_before:]):
                    ok = True
                    break
            page.wait_for_timeout(500)  # 残ダイアログの処理を確実に終わらせる
            if ok:
                task.results[key] = "成功"
            else:
                task.results[key] = f"失敗:完了未確認(dialog={dialog_log[dialog_before:]})"

        except PWTimeout:
            task.results[key] = "失敗:タイムアウト"
        except Exception as e:
            task.results[key] = f"失敗:{e}"
            logger.exception(f"skyhrs {task.kanri_no} でエラー")

    page.close()


# ----------------------------------------------------------------
# ピタクラ（https://buy.pitat-cloud.com）
# ----------------------------------------------------------------

def _dump_debug(pg, name: str) -> None:
    """ページのスクショとHTMLを保存（原因調査用）。"""
    try:
        pg.screenshot(path=str(BASE_DIR / f"{name}.png"), full_page=True)
        (BASE_DIR / f"{name}.html").write_text(pg.content(), encoding="utf-8")
        logger.info(f"  → {name}.png / {name}.html を保存しました")
    except Exception:
        pass


def _pitat_attempt(context, site_cfg: dict, task: Task, dry_run: bool) -> str:
    """
    ピタクラで1物件を1回処理する。結果ステータス文字列を返す。
    毎回ログイン後ページから検索し直す（リトライ時もクリーンに再実行するため）。
    成功/スキップ/DRY-RUN は確定、'失敗:...' はリトライ対象。
    """
    list_url = site_cfg["list_url"]
    home_url = site_cfg.get("home_url", list_url)
    page = context.new_page()
    detail = None
    try:
        page.goto(home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        if "login" in page.url.lower():
            return "失敗:セッション切れ(要login_setup)"
        page.goto(list_url, wait_until="domcontentloaded")

        # 検索窓が表示されるまで待つ（SPAなのでnetworkidleは使わない）
        try:
            page.wait_for_selector('input[placeholder*="入力"]', timeout=30000)
        except PWTimeout:
            _dump_debug(page, "debug_pitat")
            return "失敗:検索フォーム未表示(要確認)"
        page.wait_for_timeout(1000)

        # 検索条件: ラジオ「物件管理番号」+ 取扱店舗「指定しない」
        page.locator('label:has-text("物件管理番号")').first.click()
        page.wait_for_timeout(300)
        page.select_option('select.tenpo-select', value="0")
        page.wait_for_timeout(300)

        # 検索窓に入力 → Enter（ラジオ切替でplaceholderが変わるため*入力*で拾う）
        box = page.locator('input[placeholder*="入力"]').first
        box.click()
        box.fill("")
        box.type(task.kanri_no, delay=30)  # 1文字ずつ（SPA入力検知対策）
        page.wait_for_timeout(300)
        logger.info(f"  検索窓入力値='{box.input_value()}'")
        box.press("Enter")
        page.wait_for_timeout(3000)

        # 検索結果の「詳細」ボタン数で件数判定
        detail_btns = page.locator('button.button-basic:has-text("詳細")')
        n = detail_btns.count()
        logger.info(f"  結果{n}件 / URL={page.url}")
        if n == 0:
            _dump_debug(page, "debug_pitat")
            return "スキップ:0件(該当なし)"
        if n > 1:
            return f"スキップ:複数ヒット({n}件)→手動確認"

        if dry_run:
            return f"DRY-RUN: {'成約に変更' if task.action == 'sold' else f'価格→{task.new_price}万円'}"

        # 詳細ボタン → 新タブ／同一タブ遷移の両方に対応
        before_pages = len(context.pages)
        logger.info("  詳細ボタンをクリックします...")
        detail_btns.first.click()
        for _ in range(30):
            page.wait_for_timeout(500)
            if len(context.pages) > before_pages:
                detail = context.pages[-1]
                logger.info(f"  新タブ検出: {detail.url}")
                break
        if detail is None:
            detail = page
            logger.info(f"  新タブなし。同一タブ遷移: {page.url}")

        # フォーム準備＋入力を最大4回リトライ（新タブは検出済みなのでここだけ粘る）
        edited = False
        for attempt in range(1, 5):
            try:
                if task.action == "sold":
                    # 販売区分→販売中止（これを選ぶと中止区分が有効化される）
                    detail.wait_for_selector('label:has-text("販売中止")', timeout=5000)
                    detail.locator('label:has-text("販売中止")').first.click()
                    # 中止区分の「他決」は初期disabled。固定待ちではなく
                    # 「有効になるまで」最大8秒ポーリングしてからクリック（速度非依存）
                    otsu_radio = detail.locator('label:has-text("他決") input[type="radio"]')
                    for _ in range(16):
                        detail.wait_for_timeout(500)
                        try:
                            if not otsu_radio.is_disabled():
                                break
                        except Exception:
                            pass
                    detail.locator('label:has-text("他決")').first.click()
                    detail.wait_for_timeout(300)
                    # 念のため他決が選択されたか確認（未選択ならリトライへ）
                    if not otsu_radio.is_checked():
                        raise PWTimeout("他決の選択を確認できませんでした")
                else:
                    # 販売価格欄 = 「価格変更」ボタンと同じ行の text-right 入力
                    # （先頭の text-right は収益区分の disabled 欄なので .first ではダメ）
                    price_btn = detail.locator('button:has-text("価格変更")')
                    price_input = detail.locator('div.row').filter(
                        has=price_btn).locator('input.text-right').first
                    price_input.wait_for(state="visible", timeout=5000)
                    price_input.fill(task.new_price)
                    detail.wait_for_timeout(300)
                edited = True
                break
            except PWTimeout:
                logger.info(f"  詳細フォーム未準備（試行{attempt}/4）… 2秒待って再試行")
                detail.wait_for_timeout(2000)

        if not edited:
            _dump_debug(detail, "debug_pitat_detail")
            return "失敗:詳細フォーム未表示(要確認)"

        # 登録 → 確認ダイアログ「はい」
        detail.click('button.button-register')
        detail.wait_for_selector('.el-message-box', timeout=10000)
        detail.wait_for_timeout(300)
        detail.click('.el-message-box button.el-button--primary')

        # 完了メッセージ「登録しました。」が出るまでポーリング
        ok = False
        for _ in range(20):
            detail.wait_for_timeout(500)
            boxes = detail.locator('.el-message-box__content')
            if boxes.count():
                txt = boxes.first.inner_text()
                if "登録しました" in txt or "完了" in txt:
                    ok = True
                    btn = detail.locator('.el-message-box button.el-button--primary')
                    if btn.count():
                        btn.click()
                    break
        detail.wait_for_timeout(500)
        return "成功" if ok else "成功(完了文言未確認)"

    finally:
        # クリーンアップ: 新タブを閉じ、検索用ページも閉じる
        try:
            if detail is not None and detail is not page:
                detail.close()
        except Exception:
            pass
        try:
            page.close()
        except Exception:
            pass


def process_pitat(context, site_cfg: dict, tasks: list[Task], dry_run: bool) -> None:
    """ピタクラで各タスクを処理する。"""
    for task in tasks:
        key = "pitat"
        try:
            task.results[key] = _pitat_attempt(context, site_cfg, task, dry_run)
        except PWTimeout:
            task.results[key] = "失敗:タイムアウト"
        except Exception as e:
            task.results[key] = f"失敗:{e}"
            logger.exception(f"pitat {task.kanri_no} でエラー")


# ----------------------------------------------------------------
# メイン
# ----------------------------------------------------------------

def run_site(site_key: str, site_cfg: dict, tasks: list[Task],
             dry_run: bool, headless: bool, pause: bool = False) -> None:
    """1サイト分のブラウザを起動してタスクを処理する。"""
    auth_state = BASE_DIR / site_cfg["auth_state"]
    if not auth_state.exists():
        logger.error(f"{site_key}: セッション未保存。先に `python login_setup.py {site_key}` を実行してください。")
        for t in tasks:
            t.results[site_key] = "失敗:セッション未保存"
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(storage_state=str(auth_state))
        if site_key == "skyhrs":
            process_skyhrs(context, site_cfg, tasks, dry_run, pause=pause)
        elif site_key == "pitat":
            process_pitat(context, site_cfg, tasks, dry_run)
        browser.close()


def print_summary(tasks: list[Task], sites: list[str]) -> None:
    logger.info("\n========== 処理結果サマリー ==========")
    for t in tasks:
        act = "成約" if t.action == "sold" else f"価格変更→{t.new_price}万円"
        name = t.info.get("建物名", "") or t.info.get("所在地", "")
        logger.info(f"\n[{t.kanri_no}] {act}  {name}")
        for s in sites:
            logger.info(f"   {s}: {t.results.get(s, '未処理')}")


def main() -> None:
    ap = argparse.ArgumentParser(description="WEB自動更新（スカイヤーズ/ピタクラ）")
    ap.add_argument("--execute", action="store_true", help="実際に更新する（既定はドライラン）")
    ap.add_argument("--only", default="", help="対象の物件管理番号（カンマ区切り）")
    ap.add_argument("--limit", type=int, default=0, help="先頭N件だけ処理")
    ap.add_argument("--site", choices=["skyhrs", "pitat", "both"], default="both")
    ap.add_argument("--show", action="store_true", help="ブラウザを表示（デバッグ用）")
    ap.add_argument("--pause", action="store_true",
                    help="検索ワード入力後に停止（手動で条件確認・検索する。--show併用推奨）")
    args = ap.parse_args()

    dry_run = not args.execute

    if not RESULT_PATH.exists():
        logger.error(f"{RESULT_PATH} がありません。先に check_csv.py を実行してください。")
        sys.exit(1)

    result = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    cfg    = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    web    = cfg.get("web_update", {})

    tasks = build_tasks(result)

    # フィルタ: --only
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        tasks = [t for t in tasks if t.kanri_no in wanted]
    # フィルタ: --limit
    if args.limit > 0:
        tasks = tasks[: args.limit]

    if not tasks:
        logger.info("処理対象のタスクがありません。")
        return

    sites = ["skyhrs", "pitat"] if args.site == "both" else [args.site]

    mode = "ドライラン（実更新なし）" if dry_run else "★本番実行★"
    logger.info(f"モード: {mode} / 対象サイト: {sites} / タスク数: {len(tasks)}")
    for t in tasks:
        act = "成約" if t.action == "sold" else f"価格変更→{t.new_price}万円"
        logger.info(f"  - {t.kanri_no}: {act}")

    for site_key in sites:
        if site_key not in web:
            logger.warning(f"{site_key} は config.json にありません。スキップ。")
            continue
        logger.info(f"\n----- {site_key} 処理開始 -----")
        run_site(site_key, web[site_key], tasks, dry_run,
                 headless=not args.show, pause=args.pause)

    print_summary(tasks, sites)


if __name__ == "__main__":
    main()
