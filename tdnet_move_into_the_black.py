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
  TDNET_MOVE_INTO_THE_BLACK_WEBHOOK_URL   Slack / Discord の Incoming Webhook URL。黒字転換を通知する
  TDNET_MOVE_INTO_THE_BLACK_HEARTBEAT_WEBHOOK_URL
                                           起動・停止・ハートビートの通知先Webhook URL。
                                           黒字転換とは別のWebhookを指定する。未設定なら送信しない
  TDNET_MOVE_INTO_THE_BLACK_HEARTBEAT_SECONDS
                                           稼働中であることをWebhookへ知らせる間隔(秒, 既定3600)。
                                           0以下で無効。黒字転換が0件でも送信する
  LOG_LEVEL                               ログ出力レベル(DEBUG / INFO / WARNING / ERROR, 既定 INFO)
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
import signal
import sqlite3
import sys
import time
import unicodedata
import zipfile
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_BASE = "https://webapi.yanoshin.jp/webapi/tdnet/list"
DB_PATH = ".cache/tdnet_seen.sqlite3"
POLL_SECONDS = 60
MAX_BACKOFF_SECONDS = 1800
MAX_PARSE_ATTEMPTS = 5
HEARTBEAT_SECONDS = 3600
WEBHOOK_ENV = "TDNET_MOVE_INTO_THE_BLACK_WEBHOOK_URL"
HEARTBEAT_WEBHOOK_ENV = "TDNET_MOVE_INTO_THE_BLACK_HEARTBEAT_WEBHOOK_URL"
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

log = logging.getLogger("tdnet")

_session: requests.Session | None = None


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


@dataclass
class RunResult:
    """1サイクル分の処理結果。ログとハートビート通知の両方で使う。"""

    turnarounds: int
    total: int
    earnings: int
    seen: int
    no_xbrl: int
    no_profit: int
    parsed: int
    failed: int
    expired: int
    elapsed: float

    def summary(self) -> str:
        return (
            f"開示{self.total}件 / 決算短信{self.earnings}件 "
            f"(処理済みスキップ{self.seen}・XBRLなし{self.no_xbrl}"
            f"・利益項目を読めず{self.no_profit}・解析成功{self.parsed}"
            f"・失敗{self.failed})"
        )


# ---------------------------------------------------------------- HTTP
def get_session() -> requests.Session:
    """接続を使い回し、一時的な通信失敗を自動リトライするセッション。"""
    global _session
    if _session is None:
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        session = requests.Session()
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        session.headers.update(HEADERS)
        _session = session
    return _session


def _describe_response(resp: requests.Response | None) -> str:
    if resp is None:
        return "応答なし"
    return f"HTTP {resp.status_code} {resp.url}"


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
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY)")
    con.execute(
        "CREATE TABLE IF NOT EXISTS failures ("
        "id TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0)"
    )
    con.commit()
    return con


def is_seen(con: sqlite3.Connection, doc_id: str) -> bool:
    return (
        con.execute("SELECT 1 FROM seen WHERE id=?", (doc_id,)).fetchone() is not None
    )


def mark_seen(con: sqlite3.Connection, doc_id: str) -> None:
    with con:
        con.execute("INSERT OR IGNORE INTO seen VALUES (?)", (doc_id,))
        con.execute("DELETE FROM failures WHERE id=?", (doc_id,))


def bump_failure(con: sqlite3.Connection, doc_id: str) -> int:
    """解析失敗回数を+1し、累計を返す。"""
    with con:
        con.execute(
            "INSERT INTO failures(id, count) VALUES(?, 1) "
            "ON CONFLICT(id) DO UPDATE SET count = count + 1",
            (doc_id,),
        )
        row = con.execute("SELECT count FROM failures WHERE id=?", (doc_id,)).fetchone()
    return int(row[0]) if row else 0


# ---------------------------------------------------------------- TDnet取得
def fetch_disclosures(date: str | None = None) -> list[dict]:
    """date: 'YYYYMMDD'。Noneなら当日分。"""
    name = f"{date}.json" if date else "today.json"
    url = f"{API_BASE}/{name}"
    r = get_session().get(url, params={"limit": 1000}, timeout=30)
    r.raise_for_status()
    try:
        payload = r.json()
    except ValueError as e:
        # 不正な日付指定などでAPIが "Invalid Request" を返すことがある
        log.error(
            "TDnet APIがJSONを返しませんでした: %s content-type=%s body=%r",
            _describe_response(r),
            r.headers.get("content-type"),
            r.text[:200],
        )
        raise RuntimeError(
            f"TDnet APIがJSONを返しませんでした: {r.text[:200]!r}"
        ) from e
    if not isinstance(payload, dict):
        raise TypeError(f"TDnet APIの応答形式が不正です: {type(payload).__name__}")

    items = payload.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        log.error(
            "TDnet APIのitemsが配列ではありません: type=%s keys=%s",
            type(items).__name__,
            list(payload),
        )
        raise TypeError(f"TDnet APIのitemsが配列ではありません: {type(items).__name__}")

    records = []
    skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        # 通常は {"Tdnet": {...}}。json2形式(ラッパーなし)や
        # 大文字小文字違い(TDnet)でも壊れないようにする。
        wrapped = next(
            (
                v
                for k, v in item.items()
                if k.lower() == "tdnet" and isinstance(v, dict)
            ),
            None,
        )
        records.append(wrapped if wrapped is not None else item)
    if skipped:
        log.warning("TDnet APIのitemsに想定外の要素が%d件ありました(スキップ)", skipped)
    log.debug(
        "TDnet API取得: %s 開示=%d件 (total_count=%s)",
        url,
        len(records),
        payload.get("total_count"),
    )
    return records


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
    r = get_session().get(xbrl_url, timeout=60)
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
        log.info("%s / %s", item.get("company_name"), item.get("title"))
        log.info("url_xbrl: %s", item.get("url_xbrl"))
        if not item.get("url_xbrl"):
            return
        r = get_session().get(item["url_xbrl"], timeout=60)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            log.info("--- zip内ファイル ---")
            for n in zf.namelist():
                log.info("%s", n)
            ixbrl = sorted(
                (n for n in zf.namelist() if n.lower().endswith("ixbrl.htm")),
                key=lambda n: ("/summary/" not in n.lower(), n),
            )
            for n in ixbrl[:2]:
                soup = BeautifulSoup(zf.read(n), "html.parser")
                seen: set = set()
                log.info("--- %s : ix:nonFraction (name, contextRef) 先頭40種 ---", n)
                for tag in soup.find_all("ix:nonfraction"):
                    key = (tag.get("name"), tag.get("contextref"))
                    if key not in seen:
                        seen.add(key)
                        log.info("%s", key)
                        if len(seen) >= 40:
                            break
        return
    log.info("該当する決算短信が見つかりません")


# ---------------------------------------------------------------- 出力・通知
def format_report(ts: list[Turnaround]) -> str:
    """同じ開示で転換した指標を1つの通知にまとめる。"""
    t0 = ts[0]
    lines = [f"【黒字転換】{t0.name} ({t0.code})  {t0.pubdate}"]
    for t in ts:
        lines.append(f"  {t.metric}: {t.prior:,.0f} → {t.current:,.0f} 百万円")
    lines += [f"  {t0.title}", f"  {t0.pdf_url}"]
    return "\n".join(lines)


def notify(text: str, env_var: str = WEBHOOK_ENV) -> None:
    """env_var に設定されたWebhookへ送る。未設定なら何もしない。"""
    url = os.environ.get(env_var)
    if not url:
        return
    # Slack は "text"、Discord は "content" を使うので両方入れる
    try:
        requests.post(url, json={"text": text, "content": text}, timeout=10)
    except requests.RequestException as e:
        log.warning("通知失敗(%s): %s: %s", env_var, type(e).__name__, e)


# ---------------------------------------------------------------- メイン処理
def run_once(
    con: sqlite3.Connection, date: str | None = None, verbose: bool = False
) -> RunResult:
    count = 0
    expired = 0
    stats = {
        "total": 0,
        "earnings": 0,
        "seen": 0,
        "no_xbrl": 0,
        "no_profit": 0,
        "parsed": 0,
        "failed": 0,
    }
    started = time.monotonic()
    for item in fetch_disclosures(date):
        stats["total"] += 1
        title = item.get("title") or ""
        if not is_earnings_release(title):
            continue
        stats["earnings"] += 1
        doc_id = str(item.get("id"))
        code = str(item.get("company_code") or "")[:4]
        name = item.get("company_name") or ""
        if is_seen(con, doc_id):
            stats["seen"] += 1
            log.debug("処理済みのためスキップ: id=%s [%s] %s", doc_id, code, name)
            continue
        try:
            turnarounds, profits, has_xbrl = find_turnarounds(item)
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                # 保存期間切れ(TDnetは約1か月)など、再試行しても直らない失敗
                mark_seen(con, doc_id)
                expired += 1
                log.info(
                    "XBRLが404のため処理済みにします(保存期間切れの可能性): "
                    "id=%s [%s] %s",
                    doc_id,
                    code,
                    name,
                )
            else:
                stats["failed"] += 1
                log.warning(
                    "XBRL取得に失敗しました(次回再試行): id=%s [%s] %s %s",
                    doc_id,
                    code,
                    name,
                    _describe_response(e.response),
                )
            continue
        except requests.RequestException as e:
            # タイムアウト・DNS・接続断など。未処理のまま次回再試行する。
            stats["failed"] += 1
            log.warning(
                "XBRL取得に失敗しました(通信エラー、次回再試行): id=%s [%s] %s %s: %s",
                doc_id,
                code,
                name,
                type(e).__name__,
                e,
            )
            continue
        except Exception as e:  # 1件の失敗で全体を止めない(未処理のまま次回再試行)
            stats["failed"] += 1
            attempts = bump_failure(con, doc_id)
            if attempts >= MAX_PARSE_ATTEMPTS:
                mark_seen(con, doc_id)
                log.error(
                    "解析に%d回失敗したため処理対象から除外します: "
                    "id=%s [%s] %s %s: %s",
                    attempts,
                    doc_id,
                    code,
                    name,
                    type(e).__name__,
                    e,
                    exc_info=log.isEnabledFor(logging.DEBUG),
                )
            else:
                log.warning(
                    "解析に失敗しました(%d/%d回目、次回再試行): id=%s [%s] %s %s: %s",
                    attempts,
                    MAX_PARSE_ATTEMPTS,
                    doc_id,
                    code,
                    name,
                    type(e).__name__,
                    e,
                    exc_info=log.isEnabledFor(logging.DEBUG),
                )
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
            log.info("  [%s] %s: %s", code, name, " / ".join(parts))
        elif verbose:
            reason = "XBRLなし" if not has_xbrl else "利益項目を読めず"
            log.info("  [%s] %s: %s", code, name, reason)

        if turnarounds:
            text = format_report(turnarounds)
            log.info("黒字転換を検出しました:\n%s", text)
            notify(text)
            count += len(turnarounds)
    result = RunResult(
        turnarounds=count,
        total=stats["total"],
        earnings=stats["earnings"],
        seen=stats["seen"],
        no_xbrl=stats["no_xbrl"],
        no_profit=stats["no_profit"],
        parsed=stats["parsed"],
        failed=stats["failed"],
        expired=expired,
        elapsed=time.monotonic() - started,
    )
    log.info("%s 所要%.1f秒", result.summary(), result.elapsed)
    if expired:
        log.info(
            "%d件はXBRLが404でした"
            "(保存期間切れの可能性。直近1か月以内の日付を指定してください)",
            expired,
        )
    return result


def configure_logging(verbose: bool) -> None:
    level_name = os.environ.get("LOG_LEVEL", "DEBUG" if verbose else "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    # HTTPライブラリの内部ログは通常は抑制する
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def format_duration(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, sec = divmod(rem, 60)
    if days:
        return f"{days}日{hours}時間{minutes}分"
    if hours:
        return f"{hours}時間{minutes}分"
    return f"{minutes}分{sec}秒"


def format_startup(interval: int, heartbeat_seconds: int, date: str | None) -> str:
    heartbeat = (
        f"{format_duration(heartbeat_seconds)}ごと" if heartbeat_seconds > 0 else "無効"
    )
    return "\n".join(
        [
            "【起動】TDnet監視サーバーを起動しました",
            f"  監視間隔: {interval}秒",
            f"  ハートビート: {heartbeat}",
            f"  対象日: {date or '当日'}",
        ]
    )


def format_heartbeat(
    cycle: int,
    uptime: int,
    result: RunResult | None,
    last_error: str | None,
    consecutive_failures: int,
) -> str:
    lines = [
        "【稼働中】TDnet監視サーバーは動作しています",
        f"  最終チェック: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if last_error:
        lines.append(
            f"  直近のチェック: 失敗({consecutive_failures}回連続) {last_error}"
        )
    elif result is not None:
        lines.append(f"  直近の黒字転換: {result.turnarounds}件")
        lines.append(f"  {result.summary()}")
        lines.append(f"  所要: {result.elapsed:.1f}秒")
    else:
        lines.append("  直近のチェック: まだ完了していません")
    lines.append(f"  連続稼働: {format_duration(uptime)} (cycle {cycle})")
    return "\n".join(lines)


def format_shutdown(uptime: int, cycle: int, reason: str) -> str:
    return "\n".join(
        [
            f"【停止】TDnet監視サーバーを停止しました({reason})",
            f"  連続稼働: {format_duration(uptime)} (cycle {cycle})",
            "  再起動されるまでハートビートは届きません",
        ]
    )


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "%s の値が数値ではありません(%r)。既定値%dを使います", name, raw, default
        )
        return default


def _raise_keyboard_interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


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
    ap.add_argument(
        "--interval",
        type=int,
        default=POLL_SECONDS,
        help=f"常駐時の監視間隔(秒, 既定 {POLL_SECONDS})",
    )
    args = ap.parse_args()

    load_dotenv()
    configure_logging(args.verbose)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    interval = max(1, args.interval)
    heartbeat_seconds = env_int(
        "TDNET_MOVE_INTO_THE_BLACK_HEARTBEAT_SECONDS", HEARTBEAT_SECONDS
    )
    heartbeat_seconds = max(0, heartbeat_seconds)

    if args.debug_code:
        debug_dump(args.date, args.debug_code)
        return

    con = init_db()
    log.info(
        "監視を開始します: 間隔=%d秒 heartbeat=%s db=%s "
        "webhook=%s heartbeat_webhook=%s date=%s",
        interval,
        f"{heartbeat_seconds}秒" if heartbeat_seconds > 0 else "無効",
        DB_PATH,
        "設定あり" if os.environ.get(WEBHOOK_ENV) else "未設定",
        "設定あり" if os.environ.get(HEARTBEAT_WEBHOOK_ENV) else "未設定",
        args.date or "当日",
    )
    if not args.once:
        notify(
            format_startup(interval, heartbeat_seconds, args.date),
            HEARTBEAT_WEBHOOK_ENV,
        )

    started = time.monotonic()
    cycle = 0
    consecutive_failures = 0
    last_heartbeat = time.monotonic()
    last_result: RunResult | None = None
    last_error: str | None = None
    stop_reason = ""
    while True:
        cycle += 1
        try:
            result = run_once(con, args.date, args.verbose)
        except KeyboardInterrupt:
            stop_reason = "シグナル受信"
            log.info("停止しました(シグナル受信)")
            break
        except Exception as e:  # 一時的なAPI異常などで常駐が落ちないように
            consecutive_failures += 1
            last_error = f"{type(e).__name__}: {e}"
            exponent = min(consecutive_failures - 1, 6)
            delay = min(interval * (2**exponent), MAX_BACKOFF_SECONDS)
            log.exception(
                "[cycle %d] 監視処理に失敗しました(%d回連続): %s "
                "/ %.0f秒後に再試行します",
                cycle,
                consecutive_failures,
                type(e).__name__,
                delay,
            )
            if args.once:
                raise
        else:
            consecutive_failures = 0
            last_result = result
            last_error = None
            delay = interval
            log.info(
                "[cycle %d] チェック完了(黒字転換 %d件) / 連続稼働 %s",
                cycle,
                result.turnarounds,
                format_duration(int(time.monotonic() - started)),
            )

        if (
            heartbeat_seconds > 0
            and time.monotonic() - last_heartbeat >= heartbeat_seconds
        ):
            notify(
                format_heartbeat(
                    cycle,
                    int(time.monotonic() - started),
                    last_result,
                    last_error,
                    consecutive_failures,
                ),
                HEARTBEAT_WEBHOOK_ENV,
            )
            last_heartbeat = time.monotonic()

        if args.once:
            break
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            stop_reason = "シグナル受信"
            log.info("停止しました(シグナル受信)")
            break

    if not args.once:
        notify(
            format_shutdown(
                int(time.monotonic() - started), cycle, stop_reason or "終了"
            ),
            HEARTBEAT_WEBHOOK_ENV,
        )
    con.close()


if __name__ == "__main__":
    main()
