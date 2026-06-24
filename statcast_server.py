"""
V's Dugout — Statcast backend
=============================
A tiny service that pulls Baseball Savant + FanGraphs once (then caches) and
serves the batted-ball metrics as JSON keyed by MLBAM player_id, so the static
V's Dugout page can auto-fill the columns a browser can't fetch itself.

WHY THIS EXISTS
  Baseball Savant has no open CORS and serves bulk data for server-side use
  (that's what pybaseball wraps). So the columns xwOBA / Barrel / HH% / LA /
  SwStr% / FB% / Pitches etc. cannot be fetched from the browser. This service
  does it server-side and hands the result to the page.

WHAT IT COVERS (current pybaseball leaderboards)
  BATTERS
    Savant expected stats ....... xwOBA (est_woba), xBA, xSLG
    Savant exit-velo & barrels ... Brl/BIP% (brl_percent), Sweetspot%, HH%, LA, barrel
    FanGraphs .................... FB%, SwStr%, Pitches
  PITCHERS (against)
    Savant expected stats ....... xwOBA-against (est_woba), xBA, xSLG
    Savant exit-velo & barrels ... Brl/BIP%, Sweetspot%, HH%, LA
    FanGraphs .................... SwStr%, FB%, CSW%, Pitches
  NOT cleanly available ........ xwOBAcon, pulled-barrel%  -> returned null

RESPONSE SHAPE
  { "batters":  { "<mlbam_id>": {metric: value, ...}, ... },
    "pitchers": { "<mlbam_id>": {metric: value, ...}, ... } }
  (separate maps so two-way players like Ohtani don't collide)

RUN LOCALLY
  pip install flask flask-cors pybaseball pandas
  python statcast_server.py
  # -> http://localhost:8000/statcast?season=2026
  # paste that URL into V's Dugout (Tools -> Load Statcast -> Auto mode)

DEPLOY (Render / Railway, free tier works)
  - Push this file + a requirements.txt:
        flask
        flask-cors
        pybaseball
        pandas
        gunicorn
  - Start command:  gunicorn statcast_server:app --timeout 120
  - First request is slow (it scrapes + caches); later requests are instant
    until the cache TTL expires. Tighten CORS origins for production.
"""
from flask import Flask, jsonify, request
from flask_cors import CORS
import time, threading
import pandas as pd
import requests

try:
    import pybaseball as pyb
    pyb.cache.enable()  # pybaseball's own on-disk cache
except Exception as e:  # allow the file to import even if not installed yet
    pyb = None
    print("pybaseball not available:", e)

app = Flask(__name__)
CORS(app)  # allow any origin; restrict with CORS(app, origins=["https://yoursite"]) in prod

_CACHE = {}            # season -> {"ts": float, "data": dict}
_TTL = 6 * 3600        # refresh every 6 hours
_LOCK = threading.Lock()
LAST = {"errors": {}, "counts": {}}   # diagnostics surfaced by /health


def _find(df, *cands):
    """Return the first column whose name matches any candidate (case-insensitive,
    exact then 'contains'). FanGraphs/Savant rename columns between versions, so we
    don't hard-code a single spelling."""
    cols = list(df.columns)
    low = {str(c).strip().lower(): c for c in cols}
    for cand in cands:
        if cand.lower() in low:
            return low[cand.lower()]
    for cand in cands:
        for lc, orig in low.items():
            if cand.lower() in lc:
                return orig
    return None


def _f(v):
    """float or None"""
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except Exception:
        return None


# ---- FanGraphs via its modern JSON API (the page pybaseball scrapes now 403s) ----
FG_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.fangraphs.com/leaders/major-league",
}


def _fg_api(stats, season, type_):
    """stats='bat'|'pit'. Returns the list of row dicts from FanGraphs' leaders API."""
    url = ("https://www.fangraphs.com/api/leaders/major-league/data"
           f"?pos=all&stats={stats}&lg=all&qual=0&type={type_}"
           f"&season={season}&season1={season}&month=0&team=0&ind=0"
           "&pageitems=2000000000&pagenum=1")
    r = requests.get(url, headers=FG_HEADERS, timeout=90)
    r.raise_for_status()
    j = r.json()
    if isinstance(j, dict):
        return j.get("data") or j.get("rows") or []
    return j if isinstance(j, list) else []


def _row_mlbam(row, fmap):
    for k in ("xMLBAMID", "MLBAMID", "mlbamid", "mlbam", "key_mlbam"):
        if k in row and row[k] not in (None, ""):
            try:
                return str(int(row[k]))
            except Exception:
                pass
    for k in ("playerid", "PlayerId", "playerId", "IDfg"):
        if k in row and row[k] not in (None, ""):
            return fmap.get(str(row[k]).strip())
    return None


def _pickv(row, *pats):
    for k in row:
        if str(k).lower() in pats:
            return row[k]
    for k in row:
        kl = str(k).lower()
        for p in pats:
            if p in kl:
                return row[k]
    return None


def _has(row, *keys):
    low = {str(k).lower() for k in row}
    return any(k in low for k in keys)


def _fg_fallback_map(rows):
    """Only needed if rows lack xMLBAMID: map FanGraphs id -> MLBAM via Chadwick."""
    if any(("xMLBAMID" in r and r.get("xMLBAMID")) for r in rows[:5]):
        return {}
    ids = []
    for r in rows:
        for k in ("playerid", "PlayerId", "IDfg"):
            if k in r and r[k] not in (None, ""):
                try:
                    ids.append(int(r[k]))
                except Exception:
                    pass
                break
    out = {}
    if pyb is None or not ids:
        return out
    try:
        rev = pyb.playerid_reverse_lookup(list(set(ids)), key_type="fangraphs")
        cF = _find(rev, "key_fangraphs")
        cM = _find(rev, "key_mlbam")
        for _, rr in rev.iterrows():
            if cF and cM and not pd.isna(rr[cF]) and not pd.isna(rr[cM]):
                out[str(int(rr[cF]))] = str(int(rr[cM]))
    except Exception as e:
        LAST["errors"]["fg_fallback_map"] = repr(e)
    return out


def _fg_collect(stats, season):
    """Pull batted-ball + plate-discipline dashboards and return merged rows.
    Tries several `type` ids; context guards (below) pick the right field, so a
    wrong type id just yields nothing rather than a wrong value."""
    rows = []
    for t in (3, 7, 2, 5):
        try:
            rows += _fg_api(stats, season=season, type_=t)
        except Exception as e:
            LAST["errors"].setdefault(f"fg_{stats}_type{t}", repr(e))
    return rows



def _pct(v):
    """FanGraphs rate columns may arrive as a fraction (0.112) or a percent (11.2).
    Normalize to a percent-number to match the page's convention."""
    f = _f(v)
    if f is None:
        return None
    return round(f * 100, 1) if abs(f) <= 1.5 else round(f, 1)


def _pid(row):
    for k in ("player_id", "mlbam", "key_mlbam", "playerid"):
        if k in row and not pd.isna(row[k]):
            try:
                return str(int(row[k]))
            except Exception:
                pass
    return None


def build(season):
    """Assemble {player_id: {metric: value}} for batters and pitchers. Each source
    is isolated so one failure doesn't sink the rest."""
    batters = {}
    pitchers = {}
    if pyb is None:
        return {"batters": batters, "pitchers": pitchers}

    def eb(pid):
        return batters.setdefault(pid, {})

    def ep(pid):
        return pitchers.setdefault(pid, {})

    # ===================== BATTERS =====================
    # Savant: expected stats (xwOBA / xBA / xSLG)
    try:
        es = pyb.statcast_batter_expected_stats(season, 25)
        for _, r in es.iterrows():
            pid = _pid(r)
            if not pid:
                continue
            d = eb(pid)
            d["xwoba"] = _f(r.get("est_woba"))
            d["xba"]   = _f(r.get("est_ba"))
            d["xslg"]  = _f(r.get("est_slg"))
    except Exception as e:
        print("batter expected_stats failed:", e)

    # Savant: exit velocity & barrels (Brl/BIP%, Sweetspot%, HH%, LA, barrel)
    try:
        ev = pyb.statcast_batter_exitvelo_barrels(season, 25)
        for _, r in ev.iterrows():
            pid = _pid(r)
            if not pid:
                continue
            d = eb(pid)
            brl = r.get("brl_percent")
            if brl is None or pd.isna(brl):
                brl = r.get("barrel_batted_rate")
            d["brlbip"]    = _pct(brl) if (brl is not None and not pd.isna(brl) and abs(_f(brl) or 0) <= 1.5) else _f(brl)
            d["barrel"]    = d["brlbip"]
            d["sweetspot"] = _f(r.get("anglesweetspotpercent"))
            d["hh"]        = _f(r.get("ev95percent"))
            d["la"]        = _f(r.get("avg_hit_angle"))
    except Exception as e:
        print("batter exitvelo_barrels failed:", e)

    # FanGraphs batting via JSON API: FB% (fly ball), SwStr%, Pitches
    try:
        rows = _fg_collect("bat", season)
        fmap = _fg_fallback_map(rows)
        nb = 0
        for row in rows:
            pid = _row_mlbam(row, fmap)
            if not pid:
                continue
            d = eb(pid)
            # FB% only from a batted-ball context (guard against fastball% in pitch-type tabs)
            if _has(row, "gb%", "ld%"):
                v = _pickv(row, "fb%")
                if v is not None:
                    d["fb"] = _pct(v); nb += 1
            # SwStr%/Pitches only from a plate-discipline context
            if _has(row, "o-swing%", "contact%", "swstr%"):
                sw = _pickv(row, "swstr%")
                if sw is not None:
                    d["swstr"] = _pct(sw)
                pit = _pickv(row, "pitches")
                if pit is not None:
                    try:
                        d["pitches"] = int(float(pit))
                    except Exception:
                        pass
        LAST["counts"]["fg_batting_matched"] = nb
    except Exception as e:
        LAST["errors"]["fangraphs_batting"] = repr(e)
        print("fangraphs batting failed:", e)

    # ===================== PITCHERS =====================
    # Savant: expected stats against (xwOBA-against)
    try:
        pes = pyb.statcast_pitcher_expected_stats(season, 25)
        for _, r in pes.iterrows():
            pid = _pid(r)
            if not pid:
                continue
            d = ep(pid)
            d["xwoba"] = _f(r.get("est_woba"))
            d["xba"]   = _f(r.get("est_ba"))
            d["xslg"]  = _f(r.get("est_slg"))
    except Exception as e:
        print("pitcher expected_stats failed:", e)

    # Savant: exit velocity & barrels allowed (Brl/BIP%, Sweetspot%, HH%, LA)
    try:
        pev = pyb.statcast_pitcher_exitvelo_barrels(season, 25)
        for _, r in pev.iterrows():
            pid = _pid(r)
            if not pid:
                continue
            d = ep(pid)
            brl = r.get("brl_percent")
            if brl is None or pd.isna(brl):
                brl = r.get("barrel_batted_rate")
            d["brlbip"]    = _pct(brl) if (brl is not None and not pd.isna(brl) and abs(_f(brl) or 0) <= 1.5) else _f(brl)
            d["sweetspot"] = _f(r.get("anglesweetspotpercent"))
            d["hh"]        = _f(r.get("ev95percent"))
            d["la"]        = _f(r.get("avg_hit_angle"))
    except Exception as e:
        print("pitcher exitvelo_barrels failed:", e)

    # FanGraphs pitching via JSON API: FB% (fly ball allowed), SwStr%, CSW%, Pitches
    try:
        rows = _fg_collect("pit", season)
        fmap = _fg_fallback_map(rows)
        nb = 0
        for row in rows:
            pid = _row_mlbam(row, fmap)
            if not pid:
                continue
            d = ep(pid)
            if _has(row, "gb%", "ld%"):
                v = _pickv(row, "fb%")
                if v is not None:
                    d["fb"] = _pct(v); nb += 1
            if _has(row, "o-swing%", "contact%", "swstr%", "csw%"):
                sw = _pickv(row, "swstr%")
                if sw is not None:
                    d["swstr"] = _pct(sw)
                csw = _pickv(row, "csw%")
                if csw is not None:
                    d["csw"] = _pct(csw)
                pit = _pickv(row, "pitches")
                if pit is not None:
                    try:
                        d["pitches"] = int(float(pit))
                    except Exception:
                        pass
        LAST["counts"]["fg_pitching_matched"] = nb
    except Exception as e:
        LAST["errors"]["fangraphs_pitching"] = repr(e)
        print("fangraphs pitching failed:", e)

    # columns we can't source cleanly yet -> explicit null so the page shows "—"
    for d in batters.values():
        d.setdefault("xwobacon", None)
        d.setdefault("pulledbrl", None)
    for d in pitchers.values():
        d.setdefault("pulledbrl", None)

    return {"batters": batters, "pitchers": pitchers}


def _fg_map(df):
    """Map FanGraphs id -> MLBAM player id. Returns (mapping, id_column_name)."""
    cID = _find(df, "idfg", "playerid", "fangraphs id")
    if not cID:
        LAST["errors"]["fg_map"] = "no FanGraphs id column found"
        return {}, None
    try:
        idfg = [int(x) for x in df[cID].dropna().unique()]
        rev = pyb.playerid_reverse_lookup(idfg, key_type="fangraphs")
        cF = _find(rev, "key_fangraphs")
        cM = _find(rev, "key_mlbam")
        out = {}
        for _, r in rev.iterrows():
            if cF is None or cM is None or pd.isna(r.get(cF)) or pd.isna(r.get(cM)):
                continue
            out[int(r[cF])] = str(int(r[cM]))
        LAST["counts"]["fg_id_mapped"] = len(out)
        return out, cID
    except Exception as e:
        LAST["errors"]["fg_map"] = repr(e)
        print("id map failed:", e)
        return {}, cID


def get_season(season):
    now = time.time()
    with _LOCK:
        c = _CACHE.get(season)
        if c and now - c["ts"] < _TTL:
            return c["data"]
    data = build(season)            # build outside the lock (slow scrape)
    with _LOCK:
        _CACHE[season] = {"ts": time.time(), "data": data}
    return data


@app.route("/statcast")
def statcast():
    try:
        season = int(request.args.get("season", 2026))
    except Exception:
        season = 2026
    return jsonify(get_season(season))


@app.route("/health")
def health():
    try:
        season = int(request.args.get("season", 2026))
    except Exception:
        season = 2026
    data = get_season(season)
    b, p = data.get("batters", {}), data.get("pitchers", {})

    def cov(d, key):
        return sum(1 for v in d.values() if v.get(key) is not None)

    return jsonify({
        "ok": True,
        "pybaseball": pyb is not None,
        "season": season,
        "batters": len(b),
        "pitchers": len(p),
        "batter_coverage": {k: cov(b, k) for k in
            ["xwoba", "brlbip", "hh", "la", "sweetspot", "fb", "swstr", "pitches", "xwobacon", "pulledbrl"]},
        "pitcher_coverage": {k: cov(p, k) for k in
            ["xwoba", "brlbip", "hh", "swstr", "fb", "csw", "pitches"]},
        "diagnostics": LAST,
    })


@app.route("/")
def root():
    return ("V's Dugout Statcast backend. Try /statcast?season=2026", 200)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
