"""FastAPI backend: serves the static report + a per-stock 融資融券／三大法人 lookup API.

The lookup fetches the last N trading days for a given stock code. Both
twse.margin_stocks(date) and twse.institutional_stocks(date) return the WHOLE
market in a single call, so results are cached per-date (not per-stock) —
looking up a second stock for dates already fetched for a first stock is
instant, no new requests. A per-date lock prevents concurrent requests from
stampeding TWSE for the same date.
"""
import datetime as dt
import threading
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from twchips import twse

app = FastAPI()

ROOT_DIR = __import__("pathlib").Path(__file__).parent
LOOKBACK_DAYS = 20
MAX_CALENDAR_SPAN = 45  # safety cap in case of an unusually long holiday run

_cache_lock = threading.Lock()
_date_locks: dict[str, threading.Lock] = {}
_margin_cache: dict[str, "object"] = {}
_inst_cache: dict[str, "object"] = {}


def _lock_for(date_str: str) -> threading.Lock:
    with _cache_lock:
        if date_str not in _date_locks:
            _date_locks[date_str] = threading.Lock()
        return _date_locks[date_str]


def _fetch_date_tables(date_str: str):
    """Fetch (and cache) the whole-market margin_stocks + institutional_stocks
    tables for one date. Thread-safe per date."""
    if date_str in _margin_cache:
        return
    with _lock_for(date_str):
        if date_str in _margin_cache:  # re-check after acquiring lock
            return
        margin_df = twse.margin_stocks(date_str)
        inst_df = twse.institutional_stocks(date_str)
        _margin_cache[date_str] = margin_df
        _inst_cache[date_str] = inst_df


def _recent_trading_dates(days: int) -> list[str]:
    """Walk backward from yesterday collecting weekdays that actually have
    market data (skips weekends and holidays), up to `days` of them."""
    end = dt.date.today() - dt.timedelta(days=1)
    found: list[str] = []
    d = end
    scanned = 0
    while len(found) < days and scanned < MAX_CALENDAR_SPAN:
        scanned += 1
        if d.weekday() < 5:
            date_str = d.isoformat()
            _fetch_date_tables(date_str)
            if not _margin_cache[date_str].empty:
                found.append(date_str)
        d -= dt.timedelta(days=1)
    found.reverse()
    return found


def _row_for(df, code_col: str, code: str):
    if df is None or df.empty:
        return None
    matches = df[df[code_col] == code]
    if matches.empty:
        return None
    return matches.iloc[0]


@app.get("/api/stock/{code}")
def lookup_stock(code: str, days: int = LOOKBACK_DAYS):
    code = code.strip().upper()
    days = max(1, min(days, LOOKBACK_DAYS))

    dates = _recent_trading_dates(days)
    if not dates:
        raise HTTPException(status_code=502, detail="無法取得任何交易日資料，請稍後再試")

    name = None
    margin_series = []
    institutional_series = []
    for date_str in dates:
        mrow = _row_for(_margin_cache.get(date_str), "代號", code)
        irow = _row_for(_inst_cache.get(date_str), "證券代號", code)
        if name is None:
            if mrow is not None:
                name = mrow["名稱"]
            elif irow is not None:
                name = irow["證券名稱"]

        margin_series.append({
            "date": date_str,
            "financing_buy": int(mrow["融資買進"]) if mrow is not None else None,
            "financing_sell": int(mrow["融資賣出"]) if mrow is not None else None,
            "financing_balance": int(mrow["融資今日餘額"]) if mrow is not None else None,
            "financing_limit": int(mrow["融資次一營業日限額"]) if mrow is not None else None,
            "short_buy": int(mrow["融券買進"]) if mrow is not None else None,
            "short_sell": int(mrow["融券賣出"]) if mrow is not None else None,
            "short_balance": int(mrow["融券今日餘額"]) if mrow is not None else None,
            "short_limit": int(mrow["融券次一營業日限額"]) if mrow is not None else None,
        })
        institutional_series.append({
            "date": date_str,
            "foreign_net": int(irow["外陸資買賣超股數(不含外資自營商)"]) if irow is not None else None,
            "trust_net": int(irow["投信買賣超股數"]) if irow is not None else None,
            "dealer_net": int(irow["自營商買賣超股數"]) if irow is not None else None,
            "total_net": int(irow["三大法人買賣超股數"]) if irow is not None else None,
        })

    if name is None:
        raise HTTPException(status_code=404, detail=f"查無股票代號 {code} 的資料")

    return JSONResponse({
        "code": code,
        "name": name,
        "dates": dates,
        "margin": margin_series,
        "institutional": institutional_series,
    })


@app.get("/lookup")
def lookup_page():
    return FileResponse(ROOT_DIR / "lookup.html")


@app.get("/")
def index_page():
    return FileResponse(ROOT_DIR / "index.html")


app.mount("/data", StaticFiles(directory=ROOT_DIR / "data"), name="data")
