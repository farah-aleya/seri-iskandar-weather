"""
Builds the dashboard data and the SQLite database from the collected CSV files.

Outputs (into site/):
  data.json     everything the dashboard needs, including forecast accuracy metrics
  weather.db    SQLite database with all tables and a forecast_vs_actual view
  data/*.csv    copies of the raw CSV files for download

Accuracy metrics (forecast minus actual):
  MAE, RMSE and bias for temperature, humidity, rainfall and wind speed,
  for every forecast horizon (1 to 72 h) and grouped into horizon buckets.
  Rain events (>= 0.1 mm in an hour) are scored with POD, FAR, CSI, accuracy
  and the Brier score of the precipitation probability.
"""

import csv
import datetime as dt
import json
import math
import shutil
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SITE = ROOT / "site"

VARS = {
    "temperature": ("temperature_c", "Temperature", "°C"),
    "humidity": ("humidity_pct", "Humidity", "%"),
    "rain": ("precipitation_mm", "Rainfall", "mm"),
    "wind": ("wind_kmh", "Wind speed", "km/h"),
}
BUCKETS = [(1, 6, "1-6 h"), (7, 12, "7-12 h"), (13, 24, "13-24 h"),
           (25, 48, "25-48 h"), (49, 72, "49-72 h")]
SERIES_HORIZONS = [1, 6, 24, 48, 72]
RAIN_EVENT_MM = 0.1


def read(name):
    p = DATA / f"{name}.csv"
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def stats(pairs):
    """pairs = list of (forecast, actual). Returns n, MAE, RMSE, bias."""
    pairs = [(f, a) for f, a in pairs if f is not None and a is not None]
    n = len(pairs)
    if n == 0:
        return {"n": 0, "mae": None, "rmse": None, "bias": None}
    err = [f - a for f, a in pairs]
    return {
        "n": n,
        "mae": round(sum(abs(e) for e in err) / n, 3),
        "rmse": round(math.sqrt(sum(e * e for e in err) / n), 3),
        "bias": round(sum(err) / n, 3),
    }


def rain_scores(rows):
    """rows = list of (forecast_mm, prob_pct, actual_mm)."""
    hit = miss = fa = cn = 0
    brier = []
    for f, p, a in rows:
        if a is None:
            continue
        obs = a >= RAIN_EVENT_MM
        if f is not None:
            pred = f >= RAIN_EVENT_MM
            if pred and obs:
                hit += 1
            elif obs:
                miss += 1
            elif pred:
                fa += 1
            else:
                cn += 1
        if p is not None:
            brier.append((p / 100 - (1 if obs else 0)) ** 2)
    total = hit + miss + fa + cn

    def ratio(a, b):
        return round(a / b, 3) if b else None

    return {
        "hits": hit, "misses": miss, "false_alarms": fa, "correct_negatives": cn,
        "pod": ratio(hit, hit + miss),          # probability of detection
        "far": ratio(fa, hit + fa),             # false alarm ratio
        "csi": ratio(hit, hit + miss + fa),     # critical success index
        "accuracy": ratio(hit + cn, total),
        "brier": round(sum(brier) / len(brier), 3) if brier else None,
        "n": total,
    }


def rain_alert(next_hours):
    """Same rule as the ESP32 rain-alert device."""
    probs = [num(r["precip_prob_pct"]) or 0 for r in next_hours]
    mm = sum(num(r["precipitation_mm"]) or 0 for r in next_hours)
    pmax = max(probs) if probs else 0
    if pmax >= 70 or mm >= 1.0:
        level, text = "red", "Rain very likely in the next 3 hours"
    elif pmax >= 40 or mm >= 0.2:
        level, text = "amber", "Rain possible in the next 3 hours"
    else:
        level, text = "green", "No rain expected in the next 3 hours"
    return {"level": level, "text": text, "max_prob_pct": pmax, "total_mm": round(mm, 1)}


def collection_status(runs, obs_times):
    ok = [r for r in runs if r["status"] == "ok"]
    err = [r for r in runs if r["status"] != "ok"]
    issues = sorted({r["issued_at"] for r in ok if r["issued_at"]})
    if not issues:
        return {}
    hours = [dt.datetime.fromisoformat(t) for t in issues]
    first, last = hours[0], hours[-1]
    expected = int((last - first).total_seconds() // 3600) + 1
    # longest continuous stretch (no gap of more than 2 hours between stored forecasts)
    best = cur = 0.0
    for a, b in zip(hours, hours[1:]):
        gap = (b - a).total_seconds() / 3600
        cur = cur + gap if gap <= 2 else 0
        best = max(best, cur)
    return {
        "first_issue": issues[0],
        "last_issue": issues[-1],
        "hours_with_forecast": len(issues),
        "hours_expected": expected,
        "coverage_pct": round(100 * len(issues) / expected, 1),
        "longest_continuous_h": round(best, 1),
        "runs_ok": len(ok),
        "runs_failed": len(err),
        "observation_hours": len(obs_times),
    }


def build_db(path, tables):
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE observations (
        time TEXT PRIMARY KEY, temperature_c REAL, humidity_pct REAL,
        precipitation_mm REAL, wind_kmh REAL, updated_at_utc TEXT);
    CREATE TABLE forecasts (
        issued_at TEXT, target_time TEXT, horizon_h INTEGER, temperature_c REAL,
        humidity_pct REAL, precipitation_mm REAL, precip_prob_pct REAL, wind_kmh REAL,
        PRIMARY KEY (issued_at, target_time));
    CREATE INDEX idx_fc_target ON forecasts(target_time);
    CREATE TABLE daily_forecasts (
        issued_at TEXT, target_date TEXT, lead_days INTEGER, tmax_c REAL, tmin_c REAL,
        precip_sum_mm REAL, precip_prob_max_pct REAL, PRIMARY KEY (issued_at, target_date));
    CREATE TABLE current_readings (
        fetched_at_utc TEXT PRIMARY KEY, time TEXT, temperature_c REAL, humidity_pct REAL,
        precipitation_mm REAL, wind_kmh REAL, weather_code INTEGER);
    CREATE TABLE runs (
        run_at_utc TEXT, issued_at TEXT, status TEXT, forecast_rows INTEGER, note TEXT);
    CREATE VIEW forecast_vs_actual AS
        SELECT f.issued_at, f.target_time, f.horizon_h,
               f.temperature_c AS fc_temp, o.temperature_c AS act_temp,
               f.temperature_c - o.temperature_c AS err_temp,
               f.humidity_pct AS fc_hum, o.humidity_pct AS act_hum,
               f.humidity_pct - o.humidity_pct AS err_hum,
               f.precipitation_mm AS fc_rain, f.precip_prob_pct AS fc_rain_prob,
               o.precipitation_mm AS act_rain,
               f.wind_kmh AS fc_wind, o.wind_kmh AS act_wind,
               f.wind_kmh - o.wind_kmh AS err_wind
        FROM forecasts f JOIN observations o ON o.time = f.target_time;
    """)
    names = {"current": "current_readings"}
    for name, rows in tables.items():
        if not rows:
            continue
        cols = list(rows[0].keys())
        con.executemany(
            f"INSERT OR REPLACE INTO {names.get(name, name)} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [[(r[c] if r[c] != "" else None) for c in cols] for r in rows])
    con.commit()
    con.close()


def main():
    obs = read("observations")
    fc = read("forecasts")
    daily = read("daily_forecasts")
    cur = read("current")
    runs = read("runs")

    SITE.mkdir(exist_ok=True)
    (SITE / "data").mkdir(exist_ok=True)

    obs_by_time = {r["time"]: r for r in obs}
    # only score forecasts whose target hour has an actual value
    matched = [(f, obs_by_time[f["target_time"]]) for f in fc if f["target_time"] in obs_by_time]

    # ---- error metrics per variable, per horizon and per bucket ----
    accuracy = {}
    for key, (col, label, unit) in VARS.items():
        by_h = defaultdict(list)
        for f, o in matched:
            by_h[int(f["horizon_h"])].append((num(f[col]), num(o[col])))
        per_h = {h: stats(by_h[h]) for h in range(1, 73) if by_h[h]}
        per_bucket = []
        for lo, hi, name in BUCKETS:
            pairs = [p for h in range(lo, hi + 1) for p in by_h[h]]
            per_bucket.append({"bucket": name, **stats(pairs)})
        overall = stats([p for v in by_h.values() for p in v])
        accuracy[key] = {"label": label, "unit": unit, "overall": overall,
                         "buckets": per_bucket,
                         "by_horizon": [{"h": h, **s} for h, s in sorted(per_h.items())]}

    # ---- rain event skill ----
    rain = []
    for lo, hi, name in BUCKETS:
        rows = [(num(f["precipitation_mm"]), num(f["precip_prob_pct"]), num(o["precipitation_mm"]))
                for f, o in matched if lo <= int(f["horizon_h"]) <= hi]
        rain.append({"bucket": name, **rain_scores(rows)})
    rain_overall = rain_scores([(num(f["precipitation_mm"]), num(f["precip_prob_pct"]),
                                 num(o["precipitation_mm"])) for f, o in matched])

    # ---- forecast vs actual series for charts ----
    fc_index = defaultdict(dict)  # target_time -> horizon -> row
    for f in fc:
        h = int(f["horizon_h"])
        if h in SERIES_HORIZONS:
            fc_index[f["target_time"]][h] = f
    series = []
    for o in obs:
        t = o["time"]
        row = {"t": t}
        for key, (col, _, _) in VARS.items():
            row[key] = num(o[col])
            for h in SERIES_HORIZONS:
                ff = fc_index[t].get(h)
                row[f"{key}_f{h}"] = num(ff[col]) if ff else None
        series.append(row)
    # keep the chart to periods where forecasts were being collected
    first_issue = min((f["issued_at"] for f in fc), default=None)
    if first_issue:
        series = [s for s in series if s["t"] >= first_issue]

    # ---- latest forecast (next 24 h) and rain alert ----
    latest_issue = max((f["issued_at"] for f in fc), default=None)
    latest = sorted([f for f in fc if f["issued_at"] == latest_issue], key=lambda r: int(r["horizon_h"]))
    next24 = [{"t": f["target_time"], "temperature": num(f["temperature_c"]),
               "humidity": num(f["humidity_pct"]), "rain": num(f["precipitation_mm"]),
               "rain_prob": num(f["precip_prob_pct"]), "wind": num(f["wind_kmh"])}
              for f in latest[:24]]
    alert = rain_alert(latest[:3]) if latest else None

    # ---- daily actual summary + next-day forecast check ----
    by_day = defaultdict(list)
    for o in obs:
        by_day[o["time"][:10]].append(o)
    daily_actual = {}
    for day, rows in sorted(by_day.items()):
        if len(rows) < 24:
            continue  # only complete days
        temps = [num(r["temperature_c"]) for r in rows]
        daily_actual[day] = {
            "tmax": max(temps), "tmin": min(temps),
            "rain": round(sum(num(r["precipitation_mm"]) or 0 for r in rows), 1),
            "hum_mean": round(sum(num(r["humidity_pct"]) for r in rows) / len(rows), 1),
            "wind_max": max(num(r["wind_kmh"]) for r in rows),
        }
    next_day = []
    for day, act in daily_actual.items():
        prev = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
        cands = [d for d in daily if d["target_date"] == day and d["issued_at"].startswith(prev)]
        if not cands:
            continue
        morning = [d for d in cands if d["issued_at"][11:13] >= "08"]
        pick = min(morning or cands, key=lambda d: d["issued_at"])  # forecast issued ~08:00 the day before
        next_day.append({
            "date": day, "issued_at": pick["issued_at"],
            "tmax_f": num(pick["tmax_c"]), "tmax_a": act["tmax"],
            "tmin_f": num(pick["tmin_c"]), "tmin_a": act["tmin"],
            "rain_f": num(pick["precip_sum_mm"]), "rain_a": act["rain"],
            "rain_prob_f": num(pick["precip_prob_max_pct"]),
        })

    status = collection_status(runs, obs_by_time)
    latest_cur = cur[-1] if cur else None

    out = {
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "location": {"name": "Seri Iskandar, Perak, Malaysia", "lat": 4.36, "lon": 100.97},
        "current": latest_cur and {
            "time": latest_cur["time"],
            "temperature": num(latest_cur["temperature_c"]),
            "humidity": num(latest_cur["humidity_pct"]),
            "rain": num(latest_cur["precipitation_mm"]),
            "wind": num(latest_cur["wind_kmh"]),
            "weather_code": num(latest_cur["weather_code"]),
        },
        "alert": alert,
        "next24": next24,
        "series": series,
        "series_horizons": SERIES_HORIZONS,
        "accuracy": accuracy,
        "rain_skill": {"threshold_mm": RAIN_EVENT_MM, "overall": rain_overall, "buckets": rain},
        "daily_actual": [{"date": d, **v} for d, v in daily_actual.items()],
        "next_day": next_day,
        "status": status,
        "recent_runs": runs[-12:][::-1],
    }
    (SITE / "data.json").write_text(json.dumps(out, separators=(",", ":")))

    build_db(SITE / "weather.db", {"observations": obs, "forecasts": fc,
                                   "daily_forecasts": daily, "current": cur, "runs": runs})
    for p in DATA.glob("*.csv"):
        shutil.copy(p, SITE / "data" / p.name)
    print(f"Built dashboard data: {len(obs)} observation hours, {len(fc)} forecast rows, "
          f"{len(matched)} matched pairs")


if __name__ == "__main__":
    main()
