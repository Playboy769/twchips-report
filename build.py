"""Incrementally fetch new trading days via twchips and regenerate index.html.

Only fetches dates not already in data/history.json (fixed start 2026-04-30,
window grows forward — never rolls). Only touches the embedded data blobs
(`const A = …`, `const MAINT = …`, the #live-range/#live-days header spans) —
never the hand-written finding/thesis prose, which is a frozen snapshot as of
2026-07-31 (see the <p class="snapshot-note"> in index.html).

Safe to run repeatedly: if there are no new trading days, it's a no-op and
exits without touching index.html, so the GitHub Action won't create empty
commits.
"""
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

from twchips import taifex, twse
from twchips._core import get_json

ROOT = Path(__file__).parent
HISTORY_PATH = ROOT / "data" / "history.json"
MAINTENANCE_PATH = ROOT / "data" / "maintenance.json"
INDEX_PATH = ROOT / "index.html"

FIXED_START = dt.date(2026, 4, 30)
SLEEP = 1.0  # be polite to TWSE/TAIFEX between requests


def load_history():
    with open(HISTORY_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_maintenance():
    with open(MAINTENANCE_PATH, encoding="utf-8") as f:
        return json.load(f)


def cached_dates(history):
    return sorted({r["date"] for r in history["twse"]})


def weekdays_between(start, end):
    d = start
    out = []
    while d <= end:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def to_num(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s in ("", "--", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_maintenance_ratio(date_str):
    """Precisely computed: per-stock (financing balance × close price) summed
    across the whole market, ÷ aggregate financing amount. 3 requests total."""
    compact = date_str.replace("-", "")
    ms = twse.margin_stocks(date_str)
    time.sleep(SLEEP)
    payload = get_json(
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
        {"response": "json", "date": compact, "type": "ALL"},
    )
    time.sleep(SLEEP)
    quote_table = next(
        (t for t in payload.get("tables", []) if t.get("title") and "每日收盤行情" in t["title"]),
        None,
    )
    price_by_code = {}
    if quote_table:
        idx_code = quote_table["fields"].index("證券代號")
        idx_close = quote_table["fields"].index("收盤價")
        for row in quote_table["data"]:
            price_by_code[row[idx_code]] = to_num(row[idx_close])

    m = twse.margin(date_str)
    time.sleep(SLEEP)
    if m.empty or ms.empty:
        return None
    fin_row = m[m["項目"] == "融資金額(仟元)"].iloc[0]
    fin_amt_thousand = int(fin_row["今日餘額"])
    if fin_amt_thousand == 0:
        return None

    collateral_value = 0.0
    for _, row in ms.iterrows():
        price = price_by_code.get(row["代號"])
        lots = row["融資今日餘額"]
        if price is not None and lots:
            collateral_value += lots * 1000 * price

    ratio = collateral_value / (fin_amt_thousand * 1000) * 100
    return round(ratio, 1)


def fetch_day(date_str, history):
    """Fetch one trading day across all endpoints, append to history in place.
    Returns True if the day had any data (i.e. wasn't a market holiday)."""
    any_data = False
    try:
        df = twse.institutional(date_str)
        if not df.empty:
            any_data = True
            for _, row in df.iterrows():
                history["twse"].append({
                    "date": date_str, "category": row["單位名稱"],
                    "buy": int(row["買進金額"]), "sell": int(row["賣出金額"]), "net": int(row["買賣差額"]),
                })
        time.sleep(SLEEP)

        df2 = twse.institutional_stocks(date_str, stock="2330")
        if not df2.empty:
            r = df2.iloc[0]
            history["stock_2330"].append({
                "date": date_str,
                "foreign_net": int(r["外陸資買賣超股數(不含外資自營商)"]),
                "trust_net": int(r["投信買賣超股數"]),
                "dealer_net": int(r["自營商買賣超股數"]),
                "total_net": int(r["三大法人買賣超股數"]),
            })
        time.sleep(SLEEP)

        df3 = twse.margin(date_str)
        if not df3.empty:
            rows = {row["項目"]: row for _, row in df3.iterrows()}
            fin, short = rows["融資(交易單位)"], rows["融券(交易單位)"]
            history["margin"].append({
                "date": date_str,
                "financing_net_lots": int(fin["買進"] - fin["賣出"]),
                "short_net_lots": int(short["買進"] - short["賣出"]),
                "financing_balance": int(fin["今日餘額"]),
                "short_balance": int(short["今日餘額"]),
            })
        time.sleep(SLEEP)

        dft = taifex.institutional(date_str)
        if not dft.empty:
            any_data = True
            for _, row in dft.iterrows():
                history["taifex"].append({
                    "date": date_str, "category": row["身份別"],
                    "long_lots": int(row["多方交易口數"]), "long_amt": int(row["多方交易契約金額(百萬元)"]),
                    "short_lots": int(row["空方交易口數"]), "short_amt": int(row["空方交易契約金額(百萬元)"]),
                    "net_lots": int(row["多空交易口數淨額"]), "net_amt": int(row["多空交易契約金額淨額(百萬元)"]),
                    "oi_long_lots": int(row["多方未平倉口數"]), "oi_long_amt": int(row["多方未平倉契約金額(百萬元)"]),
                    "oi_short_lots": int(row["空方未平倉口數"]), "oi_short_amt": int(row["空方未平倉契約金額(百萬元)"]),
                    "oi_net_lots": int(row["多空未平倉口數淨額"]), "oi_net_amt": int(row["多空未平倉契約金額淨額(百萬元)"]),
                })
        time.sleep(SLEEP)

        for side in ("CALL", "PUT"):
            dfo = taifex.institutional_options(date_str, side=side, product="臺指選擇權")
            if not dfo.empty:
                for _, row in dfo.iterrows():
                    history["options"].append({
                        "date": date_str, "side": side, "category": row["身份別"],
                        "buy_lots": int(row["買方交易口數"]), "buy_amt": int(row["買方交易契約金額(千元)"]),
                        "sell_lots": int(row["賣方交易口數"]), "sell_amt": int(row["賣方交易契約金額(千元)"]),
                        "net_lots": int(row["交易口數買賣淨額"]), "net_amt": int(row["交易契約金額買賣淨額(千元)"]),
                        "oi_net_lots": int(row["未平倉口數買賣淨額"]), "oi_net_amt": int(row["未平倉契約金額買賣淨額(千元)"]),
                    })
            time.sleep(SLEEP)
    except Exception as e:
        print(f"  ! error fetching {date_str}: {e}", file=sys.stderr)
        return False

    return any_data


def compute_analysis_data(history):
    dates = sorted({r["date"] for r in history["twse"]})
    mmdd = [d[5:].replace("-", "/") for d in dates]

    twse_by_date, taifex_by_date, options_by_date = {}, {}, {}
    for r in history["twse"]:
        twse_by_date.setdefault(r["date"], {})[r["category"]] = r
    for r in history["taifex"]:
        taifex_by_date.setdefault(r["date"], {})[r["category"]] = r
    for r in history["options"]:
        if r["category"] == "外資及陸資":
            options_by_date.setdefault(r["date"], {})[r["side"]] = r
    stock_by_date = {r["date"]: r for r in history["stock_2330"]}
    margin_by_date = {r["date"]: r for r in history["margin"]}

    spot_vs_futures, dealer_split, divergence = [], [], []
    options_series, stock_series, margin_series = [], [], []

    for d in dates:
        tw = twse_by_date[d]
        tx = taifex_by_date.get(d, {})

        cash_foreign_net = tw["外資及陸資(不含外資自營商)"]["net"] + tw["外資自營商"]["net"]
        tx_foreign = tx.get("外資及陸資", {"net_amt": 0, "oi_net_amt": 0})
        spot_vs_futures.append({
            "date": d,
            "cash_foreign_net_100m": round(cash_foreign_net / 1e8, 1),
            "fut_tx_net_amt_100m": round(tx_foreign["net_amt"] / 100, 1),
            "fut_oi_net_amt_100m": round(tx_foreign["oi_net_amt"] / 100, 1),
        })

        dealer_split.append({
            "date": d,
            "prop_net_100m": round(tw["自營商(自行買賣)"]["net"] / 1e8, 1),
            "hedge_net_100m": round(tw["自營商(避險)"]["net"] / 1e8, 1),
        })

        dealer = tw["自營商(自行買賣)"]["net"] + tw["自營商(避險)"]["net"]
        trust = tw["投信"]["net"]
        foreign = tw["外資及陸資(不含外資自營商)"]["net"] + tw["外資自營商"]["net"]
        signs = [1 if v > 0 else (-1 if v < 0 else 0) for v in (dealer, trust, foreign)]
        aligned = (sum(1 for s in signs if s > 0) == 3) or (sum(1 for s in signs if s < 0) == 3)
        divergence.append({
            "date": d, "dealer_100m": round(dealer / 1e8, 1), "trust_100m": round(trust / 1e8, 1),
            "foreign_100m": round(foreign / 1e8, 1), "aligned": aligned,
        })

        o = options_by_date.get(d, {})
        if "CALL" in o and "PUT" in o:
            options_series.append({
                "date": d,
                "call_net_lots": o["CALL"]["net_lots"], "call_net_amt_100m": round(o["CALL"]["net_amt"] / 100000, 2),
                "put_net_lots": o["PUT"]["net_lots"], "put_net_amt_100m": round(o["PUT"]["net_amt"] / 100000, 2),
                "call_oi_net_lots": o["CALL"]["oi_net_lots"], "put_oi_net_lots": o["PUT"]["oi_net_lots"],
            })
        else:
            options_series.append({"date": d, "call_net_lots": 0, "call_net_amt_100m": 0, "put_net_lots": 0,
                                    "put_net_amt_100m": 0, "call_oi_net_lots": 0, "put_oi_net_lots": 0})

        s = stock_by_date.get(d)
        stock_series.append({
            "date": d,
            "foreign_net_shares": s["foreign_net"] if s else 0,
            "trust_net_shares": s["trust_net"] if s else 0,
            "dealer_net_shares": s["dealer_net"] if s else 0,
            "total_net_shares": s["total_net"] if s else 0,
        })

        m = margin_by_date.get(d)
        margin_series.append({
            "date": d,
            "financing_net_lots": m["financing_net_lots"] if m else 0,
            "short_net_lots": m["short_net_lots"] if m else 0,
            "financing_balance": m["financing_balance"] if m else 0,
            "short_balance": m["short_balance"] if m else 0,
        })

    foreign_series = [r["foreign_100m"] for r in divergence]
    mean_abs = sum(abs(v) for v in foreign_series) / len(foreign_series)
    cum = 0
    trend_outlier = []
    for r in divergence:
        cum += r["foreign_100m"]
        trend_outlier.append({
            "date": r["date"], "daily_100m": r["foreign_100m"], "cumulative_100m": round(cum, 1),
            "is_outlier": abs(r["foreign_100m"]) > 1.5 * mean_abs,
        })

    fsi_rows = [r for r in history["twse"] if r["category"] == "外資自營商"]
    fsi_always_zero = all(r["buy"] == 0 and r["sell"] == 0 and r["net"] == 0 for r in fsi_rows)

    return {
        "dates": dates, "mmdd": mmdd,
        "spot_vs_futures": spot_vs_futures, "dealer_split": dealer_split, "divergence": divergence,
        "trend_outlier": trend_outlier, "mean_abs_daily_100m": round(mean_abs, 1),
        "fsi_always_zero": fsi_always_zero, "options_series": options_series,
        "stock_series": stock_series, "margin_series": margin_series,
        "n_days": len(dates), "n_aligned": sum(1 for r in divergence if r["aligned"]),
        "n_outliers": sum(1 for r in trend_outlier if r["is_outlier"]),
        "cumulative_final_100m": round(cum, 1),
        "skipped_dates": history.get("skipped_dates", []),
    }


def inject_into_index(analysis_data, maintenance):
    html = INDEX_PATH.read_text(encoding="utf-8")

    new_json = json.dumps(analysis_data, ensure_ascii=False)
    pattern_a = re.compile(r"const A = \{.*?\};\n(\s*(?:function tooltipFor|const svgNS))", re.S)
    m = pattern_a.search(html)
    if not m:
        raise RuntimeError("could not find `const A = {...};` blob in index.html")
    html = pattern_a.sub(lambda mm: f"const A = {new_json};\n{mm.group(1)}", html, count=1)

    maint_sorted = sorted(maintenance, key=lambda r: r["date"])
    maint_js = [{"date": r["date"][5:].replace("-", "/"), "ratio": r["ratio"]} for r in maint_sorted]
    new_maint_json = json.dumps(maint_js, ensure_ascii=False)
    pattern_maint = re.compile(r"const MAINT = \[.*?\];", re.S)
    if not pattern_maint.search(html):
        raise RuntimeError("could not find `const MAINT = [...];` array in index.html")
    html = pattern_maint.sub(f"const MAINT = {new_maint_json};", html, count=1)

    date_range = f"{analysis_data['dates'][0].replace('-', '/')} – {analysis_data['dates'][-1].replace('-', '/')}"
    html = re.sub(r'(<span id="live-range">)[^<]*(</span>)', rf"\g<1>{date_range}\g<2>", html)
    html = re.sub(r'(<span id="live-days">)[^<]*(</span>)', rf"\g<1>{analysis_data['n_days']}\g<2>", html)

    INDEX_PATH.write_text(html, encoding="utf-8")


def main():
    history = load_history()
    maintenance = load_maintenance()

    have = cached_dates(history)
    last_cached = dt.date.fromisoformat(max(have)) if have else FIXED_START - dt.timedelta(days=1)
    target_end = dt.date.today() - dt.timedelta(days=1)  # yesterday: avoids "not yet published" races

    if last_cached >= target_end:
        print(f"up to date (last cached {last_cached}, target {target_end}); nothing to do")
        return 0

    candidates = weekdays_between(last_cached + dt.timedelta(days=1), target_end)
    print(f"fetching {len(candidates)} candidate weekday(s): {candidates[0]} .. {candidates[-1]}")

    maint_dates_have = {r["date"] for r in maintenance}
    new_dates = []
    for d in candidates:
        got_data = fetch_day(d, history)
        if got_data:
            new_dates.append(d)
            if d in maint_dates_have:
                print(f"  {d}: fetched (maintenance ratio already cached, skipping)")
            else:
                ratio = fetch_maintenance_ratio(d)
                if ratio is not None:
                    maintenance.append({"date": d, "ratio": ratio})
                    maint_dates_have.add(d)
                    print(f"  {d}: fetched, maintenance ratio = {ratio}%")
                else:
                    print(f"  {d}: fetched (maintenance ratio unavailable)")
        else:
            history.setdefault("skipped_dates", []).append(d)
            print(f"  {d}: no data (holiday)")

    if not new_dates:
        print("no new trading days had data; nothing to update")
        return 0

    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    MAINTENANCE_PATH.write_text(json.dumps(maintenance, ensure_ascii=False, indent=2), encoding="utf-8")

    analysis_data = compute_analysis_data(history)
    inject_into_index(analysis_data, maintenance)
    print(f"updated index.html: {analysis_data['n_days']} trading days through {analysis_data['dates'][-1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
