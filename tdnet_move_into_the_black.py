#!/usr/bin/env python3
"""
TDnet(適時開示)を監視し、決算短信から「黒字転換」した会社を列挙する。

仕組み:
  1. やのしんTDnet WebAPI(非公式)から当日の開示一覧を取得
  2. タイトルに「決算短信」を含み、未処理のものだけ対象にする
  3. 添付のXBRL(zip)内のサマリー(iXBRL)から前期・当期の利益を読む
  4. 前期 < 0 かつ 当期 > 0 (赤字→黒字) なら黒字転換として表示(+任意でWebhook通知)

使い方:
  pip install requests beautifulsoup4
  python tdnet_turnaround.py            # 常駐して監視(60秒間隔)
  python tdnet_turnaround.py --once     # 1回だけ実行
  python tdnet_turnaround.py --date 20260515 --once   # 指定日を処理

環境変数(任意)。カレントディレクトリまたはスクリプトと同じ場所の .env があれば自動で読み込む:
  TDNET_MOVE_INTO_THE_BLACK_WEBHOOK_URL   Slack / Discord の Incoming Webhook URL。設定すると通知も送る
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sqlite3
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime

import requests
from bs4 import BeautifulSoup

API_BASE = "https://webapi.yanoshin.jp/webapi/tdnet/list"
DB_PATH = ".cache/tdnet_seen.sqlite3"
POLL_SECONDS = 60
HEADERS = {"User-Agent": "tdnet-turnaround-monitor/0.1 (personal use)"}

# 判定対象の利益項目(XBRLタグのローカル名 -> 表示名)
METRICS = {
    "OperatingIncome": "営業利益",
    "OrdinaryIncome": "経常利益",
    "ProfitAttributableToOwnersOfParent": "親会社株主に帰属する当期純利益",
}

# 実績値のcontextRef(予想値は除外するため、完全一致のみ許可)
# 例: CurrentAccumulatedQ1Duration_ConsolidatedMember_ResultMember
#     CurrentYearDuration_NonConsolidatedMember_ResultMember
# 末尾が _ResultMember(実績)のものだけ対象。予想は _ForecastMember なので除外される。
CTX_RE = re.compile(
    r"^(Current|Prior)(?:Year|YTD|AccumulatedQ[1-4])Duration"
    r"(?:_(NonConsolidated|Consolidated)Member)?_ResultMember$"
)


@dataclass
class Turnaround:
    code: str
    name: str
    title: str
    pubdate: str
    pdf_url: str
    metric: str
    prior: float  # 百万円
    current: float  # 百万円


# ---------------------------------------------------------------- .env
def load_dotenv() -> None:
    """.env があれば読み込む(既に設定済みの環境変数は上書きしない)。

    探す場所: カレントディレクトリ → スクリプトと同じディレクトリ
    形式: KEY=value / export KEY=value / 引用符あり / 行頭 # はコメント
    """
    for directory in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        path = os.path.join(directory, ".env")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                line = line.removeprefix("export ")
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                elif " #" in value:  # 引用符なしの行末コメント
                    value = value.split(" #", 1)[0].rstrip()
                if key:
                    os.environ.setdefault(key, value)


# ---------------------------------------------------------------- DB
def init_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
    return con


def is_seen(con: sqlite3.Connection, doc_id: str) -> bool:
    return (
        con.execute("SELECT 1 FROM seen WHERE id=?", (doc_id,)).fetchone() is not None
    )


def mark_seen(con: sqlite3.Connection, doc_id: str) -> None:
    con.execute("INSERT OR IGNORE INTO seen VALUES (?)", (doc_id,))
    con.commit()


# ---------------------------------------------------------------- TDnet取得
def fetch_disclosures(date: str | None = None) -> list[dict]:
    """date: 'YYYYMMDD'。Noneなら当日分。"""
    name = f"{date}.json" if date else "today.json"
    r = requests.get(
        f"{API_BASE}/{name}", params={"limit": 1000}, headers=HEADERS, timeout=30
    )
    r.raise_for_status()
    return [item["Tdnet"] for item in r.json().get("items", [])]


def is_earnings_release(title: str) -> bool:
    t = unicodedata.normalize("NFKC", title)  # 全角英字を半角に
    return "決算短信" in t and "訂正" not in t and "REIT" not in t.upper()


# ---------------------------------------------------------------- XBRL解析
def _parse_value(tag) -> float | None:
    """ix:nonFraction タグから百万円単位の数値を返す。"""
    fmt = (tag.get("format") or "").lower()
    text = tag.get_text(strip=True).replace(",", "").replace("△", "-")
    if "zerodash" in fmt or text in ("", "-", "－", "―"):
        value = 0.0 if "zerodash" in fmt else None
    else:
        try:
            value = float(text)
        except ValueError:
            return None
    if value is None:
        return None
    if tag.get("sign") == "-":
        value = -abs(value)
    scale = int(tag.get("scale") or 0)
    return value * (10**scale) / 1_000_000


def extract_profits(xbrl_zip: bytes) -> dict[str, dict[str, float]]:
    """{metric: {'current': x, 'prior': y}} を返す(百万円)。"""
    with zipfile.ZipFile(io.BytesIO(xbrl_zip)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith("ixbrl.htm")]
        # サマリーを優先し、読めなければ他のiXBRLも試す
        names.sort(key=lambda n: ("/summary/" not in n.lower(), n))
        for n in names:
            result = _profits_from_html(zf.read(n))
            if result:
                return result
    return {}


def _profits_from_html(html: bytes) -> dict[str, dict[str, float]]:
    soup = BeautifulSoup(html, "html.parser")
    # {metric: {scope: {"current": x, "prior": y}}}  scope: 連結=0 / 個別=1
    raw: dict[str, dict[int, dict[str, float]]] = {}
    for tag in soup.find_all("ix:nonfraction"):
        local = (tag.get("name") or "").split(":")[-1]
        if local not in METRICS:
            continue
        m = CTX_RE.match(tag.get("contextref") or "")
        if not m:
            continue
        key = "current" if m.group(1) == "Current" else "prior"
        scope = 1 if m.group(2) == "NonConsolidated" else 0
        value = _parse_value(tag)
        if value is not None:
            raw.setdefault(local, {}).setdefault(scope, {}).setdefault(key, value)

    # 連結があれば連結、なければ個別を採用
    result: dict[str, dict[str, float]] = {}
    for local, scopes in raw.items():
        for scope in sorted(scopes):
            vals = scopes[scope]
            if "current" in vals and "prior" in vals:
                result[local] = vals
                break
    return result


def find_turnarounds(item: dict) -> tuple[list[Turnaround], dict, bool]:
    """(黒字転換リスト, 読み取った利益データ, XBRLがあったか) を返す。"""
    xbrl_url = item.get("url_xbrl")
    if not xbrl_url:
        return [], {}, False
    r = requests.get(xbrl_url, headers=HEADERS, timeout=60)
    r.raise_for_status()
    profits = extract_profits(r.content)

    found = []
    for local, vals in profits.items():
        if (
            "current" in vals
            and "prior" in vals
            and vals["prior"] < 0 < vals["current"]
        ):
            found.append(
                Turnaround(
                    code=str(item.get("company_code", ""))[:4],
                    name=item.get("company_name", ""),
                    title=item.get("title", ""),
                    pubdate=item.get("pubdate", ""),
                    pdf_url=item.get("document_url", ""),
                    metric=METRICS[local],
                    prior=vals["prior"],
                    current=vals["current"],
                )
            )
    return found, profits, True


def debug_dump(date: str | None, code: str) -> None:
    """指定銘柄の決算短信XBRLの中身(ファイル一覧とタグ)を表示する。"""
    for item in fetch_disclosures(date):
        if str(item.get("company_code", ""))[:4] != code or not is_earnings_release(
            item.get("title", "")
        ):
            continue
        print(item.get("company_name"), "/", item.get("title"))
        print("url_xbrl:", item.get("url_xbrl"))
        if not item.get("url_xbrl"):
            return
        r = requests.get(item["url_xbrl"], headers=HEADERS, timeout=60)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            print("--- zip内ファイル ---")
            for n in zf.namelist():
                print(n)
            ixbrl = sorted(
                (n for n in zf.namelist() if n.lower().endswith("ixbrl.htm")),
                key=lambda n: ("/summary/" not in n.lower(), n),
            )
            for n in ixbrl[:2]:
                soup = BeautifulSoup(zf.read(n), "html.parser")
                seen: set = set()
                print(f"--- {n} : ix:nonFraction (name, contextRef) 先頭40種 ---")
                for tag in soup.find_all("ix:nonfraction"):
                    key = (tag.get("name"), tag.get("contextref"))
                    if key not in seen:
                        seen.add(key)
                        print(key)
                        if len(seen) >= 40:
                            break
        return
    print("該当する決算短信が見つかりません")


# ---------------------------------------------------------------- 出力・通知
def format_report(ts: list[Turnaround]) -> str:
    """同じ開示で転換した指標を1つの通知にまとめる。"""
    t0 = ts[0]
    lines = [f"【黒字転換】{t0.name} ({t0.code})  {t0.pubdate}"]
    for t in ts:
        lines.append(f"  {t.metric}: {t.prior:,.0f} → {t.current:,.0f} 百万円")
    lines += [f"  {t0.title}", f"  {t0.pdf_url}"]
    return "\n".join(lines)


def notify(text: str) -> None:
    url = os.environ.get("TDNET_MOVE_INTO_THE_BLACK_WEBHOOK_URL")
    if not url:
        return
    # Slack は "text"、Discord は "content" を使うので両方入れる
    try:
        requests.post(url, json={"text": text, "content": text}, timeout=10)
    except requests.RequestException as e:
        print(f"[warn] 通知失敗: {e}")


# ---------------------------------------------------------------- メイン処理
def run_once(
    con: sqlite3.Connection, date: str | None = None, verbose: bool = False
) -> int:
    count = 0
    expired = 0
    stats = {
        "total": 0,
        "earnings": 0,
        "seen": 0,
        "no_xbrl": 0,
        "no_profit": 0,
        "parsed": 0,
    }
    for item in fetch_disclosures(date):
        stats["total"] += 1
        doc_id = str(item.get("id"))
        if not is_earnings_release(item.get("title", "")):
            continue
        stats["earnings"] += 1
        if is_seen(con, doc_id):
            stats["seen"] += 1
            continue
        try:
            turnarounds, profits, has_xbrl = find_turnarounds(item)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                # 保存期間切れ(TDnetは約1か月)など、再試行しても直らない失敗
                mark_seen(con, doc_id)
                expired += 1
            else:
                print(f"[warn] {item.get('company_name')} の取得失敗: {e}")
            continue
        except Exception as e:  # 1件の失敗で全体を止めない(未処理のまま次回再試行)
            print(f"[warn] {item.get('company_name')} の解析失敗: {e}")
            continue
        mark_seen(con, doc_id)

        if not has_xbrl:
            stats["no_xbrl"] += 1
        elif not profits:
            stats["no_profit"] += 1
        else:
            stats["parsed"] += 1
        if verbose and profits:
            parts = []
            for local, label in METRICS.items():
                v = profits.get(local, {})
                if "prior" in v and "current" in v:
                    parts.append(f"{label} {v['prior']:,.0f}→{v['current']:,.0f}")
            print(
                f"  [{item.get('company_code', '')[:4]}] {item.get('company_name')}: "
                + " / ".join(parts)
            )
        elif verbose:
            reason = "XBRLなし" if not has_xbrl else "利益項目を読めず"
            print(
                f"  [{item.get('company_code', '')[:4]}] {item.get('company_name')}: {reason}"
            )

        if turnarounds:
            text = format_report(turnarounds)
            print(text + "\n")
            notify(text)
            count += len(turnarounds)
    if verbose:
        print(
            f"[stats] 開示{stats['total']}件 / 決算短信{stats['earnings']}件"
            f"(処理済みスキップ{stats['seen']}・XBRLなし{stats['no_xbrl']}"
            f"・利益項目を読めず{stats['no_profit']}・解析成功{stats['parsed']})"
        )
    if expired:
        print(
            f"[info] {expired} 件はXBRLが404でした(保存期間切れの可能性。直近1か月以内の日付を指定してください)"
        )
    return count


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1回だけ実行して終了")
    ap.add_argument("--date", help="処理する日付 YYYYMMDD(省略時は当日)")
    ap.add_argument(
        "--verbose", action="store_true", help="各社の前期→当期の利益と内訳を表示"
    )
    ap.add_argument(
        "--debug-code",
        help="証券コード4桁を指定し、その決算短信XBRLの中身を表示して終了",
    )
    args = ap.parse_args()

    load_dotenv()

    if args.debug_code:
        debug_dump(args.date, args.debug_code)
        return

    con = init_db()
    while True:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            n = run_once(con, args.date, args.verbose)
            print(f"[{now}] チェック完了(黒字転換 {n} 件)")
        except requests.RequestException as e:
            print(f"[{now}] [warn] 取得失敗: {e}")
        if args.once:
            break
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
