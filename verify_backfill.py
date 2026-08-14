"""Post-backfill integrity check: run before committing the widened history.

Checks the history file and the two embedded blobs agree with each other and
with what the page's prose claims, so a half-finished or gap-riddled fetch
can't reach the live site unnoticed.
"""
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ANALYSIS_START = "2026-04-30"
TAIFEX_EARLIEST = "2023-08-07"

fails = []


def check(label, ok, detail=""):
    print(("OK   " if ok else "FAIL ") + label + (("  " + detail) if detail else ""))
    if not ok:
        fails.append(label)


def main():
    h = json.loads((ROOT / "data" / "history.json").read_text(encoding="utf-8"))
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    A = json.loads(re.search(r"const A = (\{.*?\});\n", html, re.S).group(1))
    L = json.loads(re.search(r"const L = (\{.*?\});\n", html, re.S).group(1))

    twse_dates = sorted({r["date"] for r in h["twse"]})
    margin_dates = {r["date"] for r in h["margin"]}
    taifex_dates = {r["date"] for r in h["taifex"]}
    skipped = set(h.get("skipped_dates", []))

    print(f"history: {len(twse_dates)} trading days  {twse_dates[0]} .. {twse_dates[-1]}")
    print(f"         margin {len(margin_dates)}, taifex {len(taifex_dates)}, skipped {len(skipped)}\n")

    # --- coverage: every weekday from 2020-01-01 is either fetched or a known holiday
    d, missing = dt.date(2020, 1, 1), []
    end = dt.date.fromisoformat(twse_dates[-1])
    while d <= end:
        if d.weekday() < 5:
            ds = d.isoformat()
            if ds not in skipped and ds not in set(twse_dates):
                missing.append(ds)
        d += dt.timedelta(days=1)
    check("no unfetched weekdays remain", not missing,
          f"{len(missing)} missing" + (f", first {missing[:3]}" if missing else ""))

    check("no duplicate twse dates", len(twse_dates) == len({r["date"] for r in h["twse"]}))
    check("margin covers every trading day", not (set(twse_dates) - margin_dates),
          f"{len(set(twse_dates) - margin_dates)} days without margin")

    # --- TAIFEX: present from its rolling-window start, absent before it
    tx_after = {d for d in twse_dates if d >= TAIFEX_EARLIEST}
    check("taifex covers every day >= " + TAIFEX_EARLIEST, not (tx_after - taifex_dates),
          f"{len(tx_after - taifex_dates)} gaps")
    check("taifex absent before " + TAIFEX_EARLIEST,
          not {d for d in taifex_dates if d < TAIFEX_EARLIEST})

    # --- const A must stay pinned to the near-term window the prose describes
    check("A starts at " + ANALYSIS_START, A["dates"][0] == ANALYSIS_START, A["dates"][0])
    check("A ends at last trading day", A["dates"][-1] == twse_dates[-1], A["dates"][-1])
    check("A day count matches prose (69)", A["n_days"] == 69, str(A["n_days"]))
    check("A.skipped_dates scoped to window", all(x >= ANALYSIS_START for x in A["skipped_dates"]))
    check("A.fsi_always_zero still true", A["fsi_always_zero"] is True)

    # --- const L: monthly, complete, nulls only where TAIFEX genuinely has none
    check("L is monthly and continuous", all(
        L["months"][i] < L["months"][i + 1] for i in range(len(L["months"]) - 1)))
    months_expected = set()
    for ds in twse_dates:
        months_expected.add(ds[:7])
    check("L covers every month in history", set(L["months"]) == months_expected,
          f"{len(months_expected - set(L['months']))} missing")
    check("L day total matches history", L["n_days"] == len(twse_dates),
          f"{L['n_days']} vs {len(twse_dates)}")
    bad_oi = [m for m, v in zip(L["months"], L["fut_oi_net_amt_100m"])
              if (v is None) != (m < TAIFEX_EARLIEST[:7])]
    check("L futures null exactly before " + TAIFEX_EARLIEST[:7], not bad_oi, str(bad_oi[:5]))
    check("L margin has no nulls", not [v for v in L["financing_balance"] if v is None])
    check("L foreign flow has no nulls", not [v for v in L["foreign_net_100m"] if v is None])

    # --- build.py's patch anchors must all still resolve
    for name, pat in [
        ("const A", r"const A = \{.*?\};\n(\s*(?:function tooltipFor|const svgNS|const L))"),
        ("const L", r"const L = \{.*?\};\n"),
        ("const MAINT", r"const MAINT = \[.*?\];"),
        ("#live-range", r'<span id="live-range">[^<]*</span>'),
        ("#live-days", r'<span id="live-days">[^<]*</span>'),
        ("#lt-range", r'<span id="lt-range">[^<]*</span>'),
        ("#lt-days", r'<span id="lt-days">[^<]*</span>'),
        ("#lt-months", r'<span id="lt-months">[^<]*</span>'),
    ]:
        check("build.py anchor " + name, bool(re.search(pat, html, re.S)))

    print()
    if fails:
        print("%d CHECK(S) FAILED: %s" % (len(fails), "; ".join(fails)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
