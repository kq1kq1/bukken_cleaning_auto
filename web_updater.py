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
  python web_updater.py --only HF000000      # 特定の物件番号だけ（複数可: HF1,HF2）
  python web_updater.py --limit 2            # 先頭2件だけ
  python web_updater.py --site skyhrs        # スカイヤーズだけ（既定: both）
  python web_updater.py --site pitat --only HF000000 --execute

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
        # 新価格は DB側=REINSで更新された価格（CSV側=現サイト価格は更新の旧値）
        tasks.append(Task(kanri_no=no, action="price",
                          new_price=str(p.get("db_価格", "")).strip(), info=p))
    return tasks


# ----------------------------------------------------------------
# 自動ログイン（セッション切れ時のフォールバック）
# ----------------------------------------------------------------

def _try_auto_login(context, page, site_key: str, site_cfg: dict) -> bool:
    """
    現在のページがログイン画面なら、config の認証情報で自動ログインする。
    成功したら storage_state を保存して True を返す。失敗時 False。

    必要な config 項目:
      - skyhrs: user_id, password
      - pitat:  gyosha_code, user_id, password
    パスワードはログに出力しない。
    """
    login_url = site_cfg.get("login_url", "")
    auth_state_path = BASE_DIR / site_cfg.get("auth_state", f"auth_state_{site_key}.json")

    # 念のため明示的にログインページへ
    try:
        page.goto(login_url, wait_until="domcontentloaded", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(1500)

    try:
        if site_key == "skyhrs":
            user_id  = str(site_cfg.get("user_id", "")).strip()
            password = str(site_cfg.get("password", "")).strip()
            if not (user_id and password):
                logger.error("skyhrs: config.json に user_id / password が無いため自動ログイン不可")
                return False
            page.fill('input[name="login_nm"]', user_id)
            page.fill('input[name="pswd"]', password)
            # 検索フォームと同様 Tab で change を発火 → submit
            page.locator('input[name="pswd"]').press("Tab")
            page.wait_for_timeout(200)
            page.click('input[type="submit"]')
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
            logger.info(f"skyhrs 自動ログイン試行: id={user_id}")

        elif site_key == "pitat":
            gyosha_code = str(site_cfg.get("gyosha_code", "")).strip()
            user_id     = str(site_cfg.get("user_id", "")).strip()
            password    = str(site_cfg.get("password", "")).strip()
            if not (gyosha_code and user_id and password):
                logger.error("pitat: config.json に gyosha_code/user_id/password が無いため自動ログイン不可")
                return False
            page.fill('#gyosha_code', gyosha_code)
            page.fill('#login_id',   user_id)
            page.fill('#password',   password)
            page.click('input[type="submit"][name="login"]')
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            page.wait_for_timeout(2500)
            logger.info(f"pitat 自動ログイン試行: gyosha={gyosha_code} / id={user_id}")
        else:
            return False
    except Exception as e:
        logger.error(f"{site_key} ログイン操作で例外: {e}")
        return False

    # 成功判定: URLに "login" が含まれていなければOK
    if "login" in page.url.lower():
        logger.error(f"{site_key} 自動ログイン失敗（loginページのまま）: {page.url}")
        return False

    # 新しいセッションを保存
    try:
        context.storage_state(path=str(auth_state_path))
        logger.info(f"{site_key} 自動ログイン成功 → セッション更新: {auth_state_path.name}")
    except Exception:
        pass
    return True


# ----------------------------------------------------------------
# スカイヤーズ（自社物件管理サイト）
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

    # 開始時にセッション確認。切れていれば自動ログインを試みる
    page.goto(search_url, wait_until="domcontentloaded")
    if "login" in page.url.lower():
        logger.info("skyhrs: セッション切れを検知。自動ログインを試行します…")
        if _try_auto_login(context, page, "skyhrs", site_cfg):
            page.goto(search_url, wait_until="domcontentloaded")
        else:
            logger.error("skyhrs: 自動ログイン失敗。全タスクをスキップします")
            for t in tasks:
                t.results["skyhrs"] = "失敗:セッション切れ(自動ログイン失敗。要login_setup)"
            page.close()
            return

    for task in tasks:
        key = "skyhrs"
        try:
            page.goto(search_url, wait_until="domcontentloaded")
            # セッション切れ判定（処理中に再び切れた場合の保険）
            if "login" in page.url.lower():
                if _try_auto_login(context, page, "skyhrs", site_cfg):
                    page.goto(search_url, wait_until="domcontentloaded")
                else:
                    task.results[key] = "失敗:セッション切れ(自動ログイン失敗)"
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
            # 複数ヒットでも全件まとめて更新する（同一物件の重複掲載が多いため）
            multi_note = f"(複数{n}件まとめて更新)" if n > 1 else ""

            if dry_run:
                act = "成約に変更" if task.action == "sold" else f"価格→{task.new_price}万円"
                task.results[key] = f"DRY-RUN: {act}{multi_note}"
                continue

            # 表示中の全行に対して目標値をセット（既に目標値の行はそのまま）。
            # スカイヤーズは変更が1つも無いと更新ボタンが反応しないため、
            # 変更件数(changed)が0ならスキップ扱いにする。
            changed = 0
            if task.action == "sold":
                for i in range(n):
                    sel = selects.nth(i)
                    if sel.input_value() != "3":
                        sel.select_option(value="3")  # 3=成約
                        changed += 1
            else:
                price_inputs = page.locator('input[name^="PRICE_FROM:"]')
                m = price_inputs.count()
                tgt = re.sub(r"[^\d]", "", str(task.new_price))
                for i in range(m):
                    inp = price_inputs.nth(i)
                    if re.sub(r"[^\d]", "", str(inp.input_value())) != tgt:
                        inp.fill(task.new_price)
                        changed += 1

            if changed == 0:
                state = "既に成約" if task.action == "sold" else "既に新価格"
                task.results[key] = f"スキップ:変更不要({state}){multi_note}"
                continue

            # 実更新
            dialog_log.clear()
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
                task.results[key] = f"成功{multi_note}"
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


def _pitat_search(page, task: Task) -> int:
    """
    共有の検索ページで1物件を検索し、ヒット件数（詳細ボタン数）を返す。
    検索条件（ラジオ「物件管理番号」+ 取扱店舗「指定しない」）を毎回セットし、
    検索窓をクリアして管理番号を入力 → Enter。ページは閉じず再利用する。
    """
    # 検索条件をセット（再検索時も念のため毎回設定。冪等）
    page.locator('label:has-text("物件管理番号")').first.click()
    page.wait_for_timeout(100)
    page.select_option('select.tenpo-select', value="0")
    page.wait_for_timeout(100)

    # 検索窓に入力 → Enter（ラジオ切替でplaceholderが変わるため*入力*で拾う）
    box = page.locator('input[placeholder*="入力"]').first
    box.click()
    box.fill("")
    box.type(task.kanri_no, delay=20)  # 1文字ずつ（SPA入力検知対策）
    # 入力値が反映されるまで100msポーリング
    for _ in range(15):
        if box.input_value().strip() == task.kanri_no:
            break
        page.wait_for_timeout(100)
    logger.info(f"  検索窓入力値='{box.input_value()}'")
    box.press("Enter")

    # 検索結果（詳細ボタン or 「該当なし」表示）が出るまで200msポーリング
    # URLが list-building に切り替わる + 結果がレンダリングされる
    list_url_seen = False
    for _ in range(30):  # 最大6秒
        page.wait_for_timeout(200)
        if "list-building" in page.url:
            list_url_seen = True
            # URL切替後、結果か0件メッセージのどちらかが出るまでもう少し待つ
            if page.locator('button.button-basic:has-text("詳細")').count() > 0:
                break
            # 0件の場合 result-area などに「該当なし」的テキストが出るが
            # 確実に判定するため、URL切替後 800ms 待って件数確定
            page.wait_for_timeout(800)
            break

    n = page.locator('button.button-basic:has-text("詳細")').count()
    logger.info(f"  結果{n}件 / URL={page.url}")
    return n


def _pitat_edit_detail(detail, task: Task) -> str:
    """
    詳細（編集）タブで成約 or 価格変更を行い、登録まで完了させる。
    結果ステータスを返す。タブの開閉は呼び出し側が行う。
    """
    # フォーム準備＋入力を最大4回リトライ（編集可能まで時間がかかるため）
    edited = False
    for attempt in range(1, 5):
        try:
            if task.action == "sold":
                # 販売区分→販売中止（これを選ぶと中止区分が有効化される）
                detail.wait_for_selector('label:has-text("販売中止")', timeout=5000)
                detail.locator('label:has-text("販売中止")').first.click()
                # 「他決」は初期disabled。有効になった瞬間に押す（200msポーリング）
                otsu_radio = detail.locator('label:has-text("他決") input[type="radio"]')
                for _ in range(40):  # 最大8秒
                    try:
                        if not otsu_radio.is_disabled():
                            break
                    except Exception:
                        pass
                    detail.wait_for_timeout(200)
                detail.locator('label:has-text("他決")').first.click()
                # 選択状態の反映を100msポーリングで確認
                for _ in range(15):  # 最大1.5秒
                    if otsu_radio.is_checked():
                        break
                    detail.wait_for_timeout(100)
                if not otsu_radio.is_checked():
                    raise PWTimeout("他決の選択を確認できませんでした")
            else:
                # 販売価格欄 = 「価格変更」ボタンと同じ行の text-right 入力
                price_btn = detail.locator('button:has-text("価格変更")')
                price_input = detail.locator('div.row').filter(
                    has=price_btn).locator('input.text-right').first
                price_input.wait_for(state="visible", timeout=5000)

                # ★重要: Vueが物件データをバインドする前に入力するとDOMだけ
                # 書き換わって reactive state は古いままになる（=登録しても保存されない）。
                # 既存価格が読み込まれる（空でなくなる）まで200msポーリングで待つ。
                for _ in range(50):  # 最大10秒
                    if price_input.input_value().strip():
                        break
                    detail.wait_for_timeout(200)

                # クリック→クリア→1文字ずつtype→Tabでblur。
                # fill() だとVueのreactive stateに反映されないケースがあるため
                # type+delay で input/change イベントを発火させる。
                price_input.click()
                price_input.fill("")
                price_input.type(task.new_price, delay=20)
                price_input.press("Tab")

                # 入力値が想定通りに反映されるまで100msポーリングで確認
                target  = re.sub(r"[^\d]", "", str(task.new_price))
                entered = ""
                for _ in range(20):  # 最大2秒
                    entered = re.sub(r"[^\d]", "", str(price_input.input_value()))
                    if entered == target:
                        break
                    detail.wait_for_timeout(100)
                if entered != target:
                    raise PWTimeout(
                        f"価格入力の確認NG: 入力={entered} / 目標={target}")
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
    # ダイアログ描画の安定化（100msポーリングで「はい」が押せるまで）
    primary_btn = detail.locator('.el-message-box button.el-button--primary')
    for _ in range(10):
        if primary_btn.count() and primary_btn.is_enabled():
            break
        detail.wait_for_timeout(100)
    primary_btn.click()

    # 完了メッセージ「登録しました。」が出るまで200msポーリング
    ok = False
    for _ in range(50):  # 最大10秒
        detail.wait_for_timeout(200)
        boxes = detail.locator('.el-message-box__content')
        if boxes.count():
            txt = boxes.first.inner_text()
            if "登録しました" in txt or "完了" in txt:
                ok = True
                btn = detail.locator('.el-message-box button.el-button--primary')
                if btn.count():
                    btn.click()
                break
    # ダイアログクローズ後の安定化（短縮）
    detail.wait_for_timeout(200)
    return "成功" if ok else "成功(完了文言未確認)"


def _pitat_one(context, page, task: Task, dry_run: bool) -> str:
    """
    共有検索ページ page を使って1物件を処理する。
    検索 → （実行時）詳細タブを開く → 更新 → 詳細タブを閉じる。
    検索ページ page は閉じない（次の物件の再検索に使い回す）。
    """
    n = _pitat_search(page, task)
    if n == 0:
        _dump_debug(page, "debug_pitat")
        return "スキップ:0件(該当なし)"
    if n > 1:
        return f"スキップ:複数ヒット({n}件)→手動確認"
    if dry_run:
        return f"DRY-RUN: {'成約に変更' if task.action == 'sold' else f'価格→{task.new_price}万円'}"

    # 「詳細」クリックで新タブが開く。新タブを取得して編集→閉じる
    detail = None
    before_pages = len(context.pages)
    logger.info("  詳細ボタンをクリックします...")
    page.locator('button.button-basic:has-text("詳細")').first.click()
    # 新タブ検出を200msポーリング（最大15秒）。開いたら即進む
    for _ in range(75):
        page.wait_for_timeout(200)
        if len(context.pages) > before_pages:
            detail = context.pages[-1]
            logger.info(f"  新タブ検出: {detail.url}")
            break
    if detail is None:
        return "失敗:詳細タブが開かない(要確認)"

    try:
        return _pitat_edit_detail(detail, task)
    finally:
        # 詳細タブだけ閉じる（検索ページは残して再検索に使う）
        try:
            detail.close()
        except Exception:
            pass


def process_pitat(context, site_cfg: dict, tasks: list[Task], dry_run: bool) -> None:
    """
    ピタクラで各タスクを処理する。
    検索ページは1枚だけ開いて使い回し、物件ごとに詳細タブを開閉する。
    """
    list_url = site_cfg["list_url"]
    home_url = site_cfg.get("home_url", list_url)
    page = context.new_page()
    try:
        # 初回のみ: home → 物件一覧 → 検索フォーム表示まで
        page.goto(home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        if "login" in page.url.lower():
            logger.info("pitat: セッション切れを検知。自動ログインを試行します…")
            if _try_auto_login(context, page, "pitat", site_cfg):
                page.goto(home_url, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
            else:
                logger.error("pitat: 自動ログイン失敗。全タスクをスキップします")
                for t in tasks:
                    t.results["pitat"] = "失敗:セッション切れ(自動ログイン失敗。要login_setup)"
                return
        page.goto(list_url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector('input[placeholder*="入力"]', timeout=30000)
        except PWTimeout:
            _dump_debug(page, "debug_pitat")
            for t in tasks:
                t.results["pitat"] = "失敗:検索フォーム未表示(要確認)"
            return
        page.wait_for_timeout(1000)

        # 各物件を、同じ検索ページで再検索しながら処理
        for task in tasks:
            try:
                task.results["pitat"] = _pitat_one(context, page, task, dry_run)
            except PWTimeout:
                task.results["pitat"] = "失敗:タイムアウト"
            except Exception as e:
                task.results["pitat"] = f"失敗:{e}"
                logger.exception(f"pitat {task.kanri_no} でエラー")
    finally:
        try:
            page.close()
        except Exception:
            pass


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
