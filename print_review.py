"""
要確認物件の印刷用HTMLを PDF（A4・モノクロ）に変換し、
Windows の既定プリンタへ送信する。

通常の起動方法:
  - 印刷.bat をダブルクリック
  - または python print_review.py

config.json で auto_print_review_after_match=true にすると、
実行.bat（check_csv.py）の照合直後に自動で呼ばれる。
"""

import os
import sys
import logging
from pathlib import Path

# Windowsコンソールでの文字化け防止
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from playwright.sync_api import sync_playwright

BASE_DIR    = Path(__file__).parent
REPORTS_DIR = BASE_DIR / "reports"
LOG_PATH    = BASE_DIR / "checker.log"

from logging.handlers import RotatingFileHandler
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=500_000, backupCount=1, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


def find_latest_review_html() -> Path | None:
    """reports/ から最新の print_review_*.html を探す。"""
    if not REPORTS_DIR.exists():
        return None
    files = sorted(
        REPORTS_DIR.glob("print_review_*.html"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[0] if files else None


def html_to_pdf(html_path: Path, pdf_path: Path, monochrome: bool = True) -> None:
    """
    HTML を A4縦の PDF に変換する。
    monochrome=True なら CSS で全要素をグレースケール化（モノクロ印刷向け）。
    """
    url = html_path.resolve().as_uri()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url, wait_until="load")
        if monochrome:
            # 全要素にグレースケールフィルタをかける（カラープリンタでもモノクロで出る）
            page.add_style_tag(content="""
                html, body, * {
                  filter: grayscale(100%) !important;
                  -webkit-filter: grayscale(100%) !important;
                }
            """)
        page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "10mm", "bottom": "10mm", "left": "10mm", "right": "10mm"},
        )
        browser.close()


def _find_sumatrapdf() -> Path | None:
    """SumatraPDF.exe のインストール場所を探す。見つからなければ None。"""
    candidates = [
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "SumatraPDF" / "SumatraPDF.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "SumatraPDF" / "SumatraPDF.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "SumatraPDF" / "SumatraPDF.exe",
        BASE_DIR / "SumatraPDF.exe",  # 同梱した場合
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def send_to_default_printer(pdf_path: Path, force_a4: bool = True, monochrome: bool = True) -> str:
    """
    既定プリンタへPDFを送信する。SumatraPDF があれば用紙サイズと白黒を強制指定。
    無ければ OS の print verb でフォールバック（プリンタの既定用紙設定が使われる）。
    戻り値: 使用した送信方法（"sumatra" / "os.startfile"）。
    """
    sumatra = _find_sumatrapdf()
    if sumatra:
        settings = []
        if force_a4:
            settings.append("paper=A4")
        if monochrome:
            settings.append("monochrome")
        cmd = [str(sumatra), "-print-to-default", "-silent"]
        if settings:
            cmd.extend(["-print-settings", ",".join(settings)])
        cmd.append(str(pdf_path))
        import subprocess
        subprocess.run(cmd, check=False)
        return "sumatra"
    # フォールバック: プリンタ既定設定が使われる
    os.startfile(str(pdf_path), "print")
    return "os.startfile"


def main() -> int:
    html = find_latest_review_html()
    if not html:
        logger.warning("印刷対象の print_review_*.html が reports/ にありません。先に実行.batでCSV照合してください。")
        return 1

    pdf = html.with_suffix(".pdf")
    logger.info(f"印刷対象HTML: {html.name}")
    logger.info(f"PDF生成中（A4・モノクロ）... → {pdf.name}")
    try:
        html_to_pdf(html, pdf, monochrome=True)
    except Exception as e:
        logger.exception(f"PDF生成失敗: {e}")
        return 2

    try:
        method = send_to_default_printer(pdf, force_a4=True, monochrome=True)
        if method == "sumatra":
            logger.info(f"SumatraPDFで既定プリンタへ送信（A4・モノクロ強制）: {pdf.name}")
        else:
            logger.info(f"OS既定で既定プリンタへ送信（用紙はプリンタ設定に従う）: {pdf.name}")
            logger.warning(
                "A4で印刷するには既定プリンタの用紙設定をA4にするか、"
                "SumatraPDFをインストールしてください（https://www.sumatrapdfreader.org/）。"
            )
    except Exception as e:
        logger.exception(f"印刷送信失敗: {e}")
        return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
