"""One-off historical backfill of the cheap datasets back to HISTORY_START.

build.py fetches six endpoints per day, which is fine for the handful of new
days it sees each night but far too slow for ~1,600 historical days. This
script fetches only the three low-cost series that TWSE/TAIFEX serve for old
dates — institutional net flows, margin balances, and (where available) TAIFEX
institutional futures — and appends them straight into data/history.json.

Deliberately NOT backfilled, because each costs extra requests per day and the
report's near-term analysis window doesn't reach back this far:
options (2 req/day), 2330 per-stock flows (1 req/day), maintenance ratio
(3 req/day).

TAIFEX serves only a rolling ~2-year window: the earliest day that returns data
is 2023-08-07, so futures cannot be backfilled to 2020 no matter how long this
runs. Dates before TAIFEX_EARLIEST skip that endpoint entirely rather than
burning a request on a guaranteed-empty response.

Resumable: writes history.json every CHECKPOINT days and skips any date already
present, so it can be killed and restarted without losing or duplicating work.
"""
import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "twchips"))
from twchips import taifex, twse  # noqa: E402

ROOT = Path(__file__).parent
HISTORY_PATH = ROOT / "data" / "history.json"

HISTORY_START = dt.date(2020, 1, 1)
# first day TAIFEX's institutional endpoint still serves (rolling ~2y window)
TAIFEX_EARLIEST = dt.date(2023, 8, 7)
SLEEP = 1.0
CHECKPOINT = 25


def weekdays(start, end):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def fetch_cheap(date_str, history, want_taifex):
    """Returns True if the day had any data (i.e. wasn't a holiday).

    All-or-nothing: rows are staged locally and only merged into `history` once
    every endpoint for the day has come back. TWSE throttles often enough that
    a mid-day failure is routine, and a half-written day is worse than no day —
    the date would then count as "present" and never be retried.
    """
    twse_rows, margin_rows, taifex_rows = [], [], []

    df = twse.institutional(date_str)
    for _, row in df.iterrows():
        twse_rows.append({
            "date": date_str, "category": row["單位名稱"],
            "buy": int(row["買進金額"]), "sell": int(row["賣出金額"]), "net": int(row["買賣差額"]),
        })
    time.sleep(SLEEP)

    if not twse_rows:
        return False  # holiday

    df3 = twse.margin(date_str)
    if not df3.empty:
        rows = {row["項目"]: row for _, row in df3.iterrows()}
        fin, short = rows["融資(交易單位)"], rows["融券(交易單位)"]
        margin_rows.append({
            "date": date_str,
            "financing_net_lots": int(fin["買進"] - fin["賣出"]),
            "short_net_lots": int(short["買進"] - short["賣出"]),
            "financing_balance": int(fin["今日餘額"]),
            "short_balance": int(short["今日餘額"]),
        })
    time.sleep(SLEEP)

    if want_taifex:
        dft = taifex.institutional(date_str)
        for _, row in dft.iterrows():
            taifex_rows.append({
                "date": date_str, "category": row["身份別"],
                "long_lots": int(row["多方交易口數"]), "long_amt": int(row["多方交易契約金額(百萬元)"]),
                "short_lots": int(row["空方交易口數"]), "short_amt": int(row["空方交易契約金額(百萬元)"]),
                "net_lots": int(row["多空交易口數淨額"]), "net_amt": int(row["多空交易契約金額淨額(百萬元)"]),
                "oi_long_lots": int(row["多方未平倉口數"]), "oi_long_amt": int(row["多方未平倉契約金額(百萬元)"]),
                "oi_short_lots": int(row["空方未平倉口數"]), "oi_short_amt": int(row["空方未平倉契約金額(百萬元)"]),
                "oi_net_lots": int(row["多空未平倉口數淨額"]), "oi_net_amt": int(row["多空未平倉契約金額淨額(百萬元)"]),
            })
        time.sleep(SLEEP)

    history["twse"].extend(twse_rows)
    history["margin"].extend(margin_rows)
    history["taifex"].extend(taifex_rows)
    return True


def drop_incomplete(history):
    """Remove days that were written partially by an earlier, non-atomic run so
    the main loop refetches them from scratch. Returns the dates dropped."""
    twse_dates = {r["date"] for r in history["twse"]}
    margin_dates = {r["date"] for r in history["margin"]}
    taifex_dates = {r["date"] for r in history["taifex"]}
    earliest_taifex = TAIFEX_EARLIEST.isoformat()

    bad = {d for d in twse_dates if d not in margin_dates}
    bad |= {d for d in twse_dates if d >= earliest_taifex and d not in taifex_dates}
    if not bad:
        return []
    for key in ("twse", "margin", "taifex"):
        history[key] = [r for r in history[key] if r["date"] not in bad]
    return sorted(bad)


def save(history):
    tmp = HISTORY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(HISTORY_PATH)


def main():
    end = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.date(2026, 4, 29)
    history = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))

    dropped = drop_incomplete(history)
    if dropped:
        print(f"dropping {len(dropped)} partially-written day(s) for refetch: "
              f"{dropped[:5]}{' …' if len(dropped) > 5 else ''}")

    have = {r["date"] for r in history["twse"]}
    skipped = set(history.get("skipped_dates", []))

    todo = [d for d in weekdays(HISTORY_START, end)
            if d.isoformat() not in have and d.isoformat() not in skipped]
    print(f"{len(todo)} weekday(s) to fetch: {todo[0]} .. {todo[-1]}" if todo else "nothing to do")
    if not todo:
        return 0

    done = holidays = errors = 0
    for i, d in enumerate(todo, 1):
        ds = d.isoformat()
        try:
            got = fetch_cheap(ds, history, want_taifex=d >= TAIFEX_EARLIEST)
        except Exception as e:
            errors += 1
            print(f"  ! {ds}: {type(e).__name__}: {str(e)[:100]}", flush=True)
            time.sleep(SLEEP * 3)
            continue
        if got:
            done += 1
        else:
            holidays += 1
            history.setdefault("skipped_dates", []).append(ds)
        if i % CHECKPOINT == 0:
            save(history)
            print(f"  [{i}/{len(todo)}] {ds} · {done} trading days, {holidays} holidays, "
                  f"{errors} errors · checkpointed", flush=True)

    save(history)
    print(f"done: {done} trading days, {holidays} holidays, {errors} errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
