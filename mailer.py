"""
メール送信モジュール（自己完結型）

Gmail SMTPを使用。config.json の notification セクションを参照。
"""

import smtplib
import logging
import traceback
from datetime import datetime
from email.message import EmailMessage

logger = logging.getLogger(__name__)

_TH = 'style="background:#f0f0f0;border:1px solid #ccc;padding:6px 10px;text-align:left;white-space:nowrap"'
_TD = 'style="border:1px solid #ddd;padding:6px 10px;vertical-align:top"'
_TD_R = 'style="border:1px solid #ddd;padding:6px 10px;text-align:right;white-space:nowrap"'
_TABLE = 'style="border-collapse:collapse;width:100%;font-size:13px;margin-bottom:24px"'


def send_email(cfg: dict, subject: str, body_html: str) -> bool:
    n = cfg.get("notification", cfg)
    email_from  = str(n.get("email_from", "")).strip()
    raw_to      = n.get("email_to", "")
    smtp_server = n.get("smtp_server", "smtp.gmail.com")
    smtp_port   = int(n.get("smtp_port", 587))
    password    = n.get("smtp_password", "")

    if isinstance(raw_to, (list, tuple)):
        to_list = [str(x).strip() for x in raw_to if str(x).strip()]
    else:
        to_list = [s.strip() for s in str(raw_to).split(",") if s.strip()]

    if not all([email_from, to_list, password]):
        logger.warning("メール設定が不完全です（config.json の notification を確認）")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"]    = email_from
    msg["To"]      = ", ".join(to_list)
    msg.set_content("HTMLメールクライアントで表示してください。")
    msg.add_alternative(body_html, subtype="html")

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(email_from, password)
            srv.send_message(msg)
        logger.info(f"メール送信成功: {subject}")
        return True
    except Exception as e:
        logger.error(f"メール送信失敗: {e}\n{traceback.format_exc()}")
        return False


def build_update_result_email(rows: list[dict]) -> tuple[str, str]:
    """
    WEB自動更新の実行結果からメール件名・本文(HTML)を生成する。

    rows の各要素（web_updater.Task を辞書化したもの）:
      kanri_no  - 物件管理番号
      action    - "sold"(成約) / "price"(価格変更)
      new_price - 価格変更時の新価格（万円）
      info      - 物件情報（建物名/所在地/最寄駅/会社名 等の比較フィールド）
      results   - {"skyhrs": "成功"/..., "pitat": "成功"/...}
    """
    now = datetime.now()

    def overall(r: dict) -> str:
        vals = list(r.get("results", {}).values())
        if vals and all(v == "成功" for v in vals):
            return "success"
        if any(str(v).startswith("失敗") for v in vals):
            return "fail"
        return "skip"

    success = [r for r in rows if overall(r) == "success"]
    fail    = [r for r in rows if overall(r) == "fail"]
    skip    = [r for r in rows if overall(r) == "skip"]

    parts = []
    if success: parts.append(f"成功{len(success)}件")
    if fail:    parts.append(f"失敗{len(fail)}件")
    if skip:    parts.append(f"スキップ{len(skip)}件")
    subject = f"[自動更新] {now:%m/%d %H:%M} " + " / ".join(parts or ["対象なし"])

    # イレギュラー（要確認）: 複数件まとめて更新 / 該当なし / 既に変更済み
    def _is_irregular(r: dict) -> bool:
        for v in r.get("results", {}).values():
            s = str(v)
            if any(k in s for k in ("複数", "0件", "該当なし", "変更不要")):
                return True
        return False

    irregular = [r for r in rows if _is_irregular(r)]

    sections = []
    if irregular:
        sections.append(_update_section(
            "⚠️ 要確認（複数件更新・該当なし・既に変更済み など）",
            irregular, "#7a4f00", "#ff9933", show_price=True,
            note="自動判定がイレギュラーだった物件です。念のため内容をご確認ください。",
        ))
    if fail:
        sections.append(_update_section("🔴 更新失敗（要手動対応）", fail, "#8b0000", "#cc0000", show_price=True))
    if success:
        sections.append(_update_section("🟢 更新成功", success, "#0a5d00", "#33aa33"))
    if skip:
        sections.append(_update_section("⚪ スキップ（変更不要・該当なし等）", skip, "#555", "#aaa", show_price=True))
    if not sections:
        sections.append("<p style='color:#555'>更新対象がありませんでした。</p>")

    subtitle = (
        f"対象: {len(rows)}件　|　"
        f"成功: {len(success)}件　|　失敗: {len(fail)}件　|　スキップ: {len(skip)}件"
    )
    body = _html_wrap(
        title=f"WEB自動更新レポート {now:%Y年%m月%d日 %H:%M}",
        subtitle=subtitle,
        content="\n".join(sections),
    )
    return subject, body


def _update_section(title: str, rows: list[dict], color: str, border: str,
                    show_price: bool = False, note: str = "") -> str:
    price_th = f"<th {_TH}>価格</th>" if show_price else ""
    head = (
        f"<th {_TH}>管理番号</th><th {_TH}>種別</th><th {_TH}>物件</th>{price_th}"
        f"<th {_TH}>スカイヤーズ</th><th {_TH}>ピタクラ</th>"
    )
    body_rows = ""
    for r in rows:
        info = r.get("info", {})
        is_sold = r.get("action") == "sold"
        act  = "成約" if is_sold else f"価格変更"
        name = info.get("csv_建物名") or info.get("建物名") or ""
        addr = info.get("csv_所在地") or info.get("所在地") or ""
        eki  = info.get("csv_最寄駅") or info.get("最寄駅") or ""
        comp = info.get("csv_会社名") or info.get("会社名") or ""
        res  = r.get("results", {})
        prop_html = (
            f"<b>{name}</b><br>"
            f"<span style='font-size:11px;color:#555'>{addr}<br>{eki}<br>{comp}</span>"
        )
        price_td = ""
        if show_price:
            if is_sold:
                price_td = f"<td {_TD_R}>{_fmt_price(info.get('csv_価格',''))}</td>"
            else:
                # 現サイト価格(csv) → 新価格(new_price=db) の方向
                price_td = (
                    f"<td {_TD_R}>{_fmt_price(info.get('csv_価格',''))}"
                    f" → <b>{_fmt_price(r.get('new_price',''))}</b></td>"
                )
        body_rows += (
            f"<tr><td {_TD}>{r.get('kanri_no','')}</td>"
            f"<td {_TD}>{act}</td>"
            f"<td {_TD}>{prop_html}</td>{price_td}"
            f"<td {_TD}>{res.get('skyhrs','-')}</td>"
            f"<td {_TD}>{res.get('pitat','-')}</td></tr>"
        )
    note_html = f"<p style='font-size:12px;color:#555;margin:4px 0 8px'>{note}</p>" if note else ""
    return f"""
<h2 style="color:{color};border-left:4px solid {border};padding-left:10px">{title}　{len(rows)}件</h2>
{note_html}
<table {_TABLE}><tr>{head}</tr>{body_rows}</table>"""


def build_check_email(result: dict, csv_name: str) -> tuple[str, str]:
    """
    照合結果からメール件名・本文(HTML)を生成する。

    result のキー:
      not_in_db      - DBに存在しない物件（成約候補）
      price_changed  - 価格変更物件
      confirmed_sold - 成約・取消シートで確認済みの物件
      matched        - 正常一致
      total_csv      - CSV件数
      total_db       - DB件数
      unmapped_fields- マッピングできなかった列
      csv_cols       - CSVの列名一覧
    """
    now     = datetime.now()
    n_miss  = len(result.get("not_in_db", []))
    n_price = len(result.get("price_changed", []))
    n_conf  = len(result.get("confirmed_sold", []))
    n_total = result.get("total_csv", 0)

    parts = []
    if n_conf:
        parts.append(f"成約確定{n_conf}件")
    if n_miss:
        parts.append(f"成約候補{n_miss}件")
    if n_price:
        parts.append(f"価格変更{n_price}件")
    if not parts:
        parts.append("変化なし")

    subject = f"[物件照合] {now:%m/%d} " + " / ".join(parts) + f"  (CSV:{n_total}件)"

    sections = []

    if result.get("confirmed_sold"):
        sections.append(_section(
            "🔴 成約確定（REINSの成約・取消シートに記録済み）",
            result["confirmed_sold"],
            color="#8b0000", border="#cc0000",
            note="REINSの成約・取消シートにすでに記録されています。自社掲載を確認してください。",
            cols=["建物名", "所在地", "価格", "会社名", "物件種別", "所在階", "土地面積", "建物面積", "専有面積"],
        ))

    if result.get("not_in_db"):
        sections.append(_section(
            "⚠️ 成約候補（REINSの物件DBに見つかりません）",
            result["not_in_db"],
            color="#7a4f00", border="#ff9933",
            note="REINSの物件DBに一致する物件が見つかりませんでした。成約済みの可能性があります。自社掲載を確認してください。",
            cols=["建物名", "所在地", "価格", "会社名", "物件種別", "所在階", "土地面積", "建物面積", "専有面積"],
        ))

    if result.get("price_changed"):
        sections.append(_section_price_changed(result["price_changed"]))

    if not sections:
        sections.append("<p style='color:#555'>照合の結果、変化のある物件はありませんでした。</p>")

    warn = ""
    if result.get("unmapped_fields"):
        warn = f"""
<div style="background:#fff3cd;border:1px solid #ffc107;padding:10px;border-radius:4px;margin-bottom:16px">
  ⚠️ <b>列マッピング警告</b>: 以下の重要フィールドがCSVで自動検出できませんでした。<br>
  未検出: <b>{', '.join(result['unmapped_fields'])}</b><br>
  CSVの列名: {', '.join(result.get('csv_cols', []))}<br>
  <small>→ column_map.json を編集して列名を指定してください。</small>
</div>"""

    subtitle = (
        f"CSV物件数: {n_total}件　|　"
        f"一致確認: {len(result.get('matched', []))}件　|　"
        f"成約確定: {n_conf}件　|　成約候補: {n_miss}件　|　価格変更: {n_price}件"
    )

    body = _html_wrap(
        title=f"物件照合レポート {now:%Y年%m月%d日}",
        subtitle=subtitle,
        content=warn + "\n".join(sections),
        csv_name=csv_name,
    )
    return subject, body


def _section(title: str, props: list[dict], color: str, border: str,
             note: str = "", cols: list[str] | None = None) -> str:
    if cols is None:
        cols = ["建物名", "所在地", "価格", "会社名", "物件種別"]
    note_html = f"<p style='font-size:12px;color:#555;margin:4px 0 8px'>{note}</p>" if note else ""
    header = "".join(f"<th {_TH}>{c}</th>" for c in cols)
    rows = ""
    for p in props:
        cells = "".join(f"<td {_TD}>{p.get(c, '')}</td>" for c in cols)
        rows += f"<tr>{cells}</tr>"
    return f"""
<h2 style="color:{color};border-left:4px solid {border};padding-left:10px">{title}　{len(props)}件</h2>
{note_html}
<table {_TABLE}><tr>{header}</tr>{rows}</table>"""


def _section_price_changed(props: list[dict]) -> str:
    note = "現サイト価格(CSV)とREINS新価格(DB)が異なります。自動更新でサイトをREINSの新価格に揃えます。"
    note_html = f"<p style='font-size:12px;color:#555;margin:4px 0 8px'>{note}</p>"
    rows = ""
    for p in props:
        csv_p    = p.get("csv_価格", "")  # 現サイト価格
        db_p     = p.get("db_価格", "")   # 新REINS価格（=更新目標）
        db_comp  = p.get("会社名", "")
        csv_comp = p.get("csv_会社名", "")
        comp_html = _comp_cell(db_comp, csv_comp)
        rows += f"""
<tr>
  <td {_TD}>{p.get('建物名', '')}</td>
  <td {_TD}>{p.get('所在地', '')}</td>
  <td {_TD}>{comp_html}</td>
  <td {_TD}>{p.get('物件種別', '')}</td>
  <td {_TD_R}>{_fmt_price(csv_p)}</td>
  <td {_TD_R}><b>{_fmt_price(db_p)}</b> {_arrow(csv_p, db_p)}</td>
</tr>"""
    header = (
        f"<th {_TH}>建物名</th><th {_TH}>所在地</th><th {_TH}>会社名（DB / CSV）</th>"
        f"<th {_TH}>種別</th><th {_TH}>現サイト価格</th><th {_TH}>新REINS価格（目標）</th>"
    )
    return f"""
<h2 style="color:#7a4f00;border-left:4px solid #e6ac00;padding-left:10px">💰 価格変更　{len(props)}件</h2>
{note_html}
<table {_TABLE}><tr>{header}</tr>{rows}</table>"""


def _comp_cell(db_comp: str, csv_comp: str) -> str:
    """DB会社名 / CSV会社名 を表示。異なる場合は黄色ハイライト。"""
    if not csv_comp:
        return db_comp
    if db_comp == csv_comp:
        return db_comp
    style = "background:#fff9c4;padding:2px 4px;border-radius:3px;font-size:11px"
    return (
        f"<span>DB: {db_comp}</span><br>"
        f"<span style='{style}'>CSV: {csv_comp}</span>"
    )


def _fmt_price(s: str) -> str:
    """数値文字列を "○○万円" 形式に変換"""
    import re
    n = re.sub(r'[^\d.]', '', str(s).replace(',', ''))
    if not n:
        return s
    try:
        return f"{float(n):.0f}万円"
    except Exception:
        return s


def _arrow(old: str, new: str) -> str:
    import re
    def to_f(s):
        s = re.sub(r'[^\d.]', '', str(s).replace(',', ''))
        try:
            return float(s)
        except Exception:
            return None
    o, n = to_f(old), to_f(new)
    if o and n:
        if n < o:
            return f'<span style="color:green">▼{o-n:.0f}万円</span>'
        elif n > o:
            return f'<span style="color:red">▲{n-o:.0f}万円</span>'
    return ""


def _html_wrap(title: str, subtitle: str, content: str, csv_name: str = "") -> str:
    csv_note = f"<p style='font-size:12px;opacity:0.75'>対象CSV: {csv_name}</p>" if csv_name else ""
    return f"""<!DOCTYPE html>
<html lang="ja">
<head><meta charset="utf-8">
<style>
  body {{ font-family:"Meiryo","Hiragino Sans",sans-serif; color:#222; max-width:960px; margin:0 auto; padding:20px; }}
  .header {{ background:#1c4587; color:white; padding:16px 20px; border-radius:6px; margin-bottom:20px; }}
  .header h1 {{ margin:0; font-size:18px; }}
  .header p  {{ margin:6px 0 0; font-size:13px; opacity:0.85; }}
  table tr:nth-child(even) {{ background:#f9f9f9; }}
  .footer {{ font-size:11px; color:#999; margin-top:30px; border-top:1px solid #eee; padding-top:10px; }}
</style>
</head>
<body>
<div class="header">
  <h1>{title}</h1>
  <p>{subtitle}</p>
  {csv_note}
</div>
{content}
<div class="footer">このレポートは物件照合ツール（bukken_cleaning_auto）により自動生成されました。</div>
</body>
</html>"""
