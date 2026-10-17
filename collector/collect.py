"""
Seri Iskandar weather collector.

Runs on a schedule (GitHub Actions, every 30 minutes). Each run:
  1. Calls the Open-Meteo forecast API once for Seri Iskandar, Perak.
  2. Saves the current conditions            -> data/current.csv        (append)
  3. Saves the past hours as "actual" values -> data/observations.csv   (upsert by hour)
  4. Saves the next 72 hourly forecasts      -> data/forecasts.csv      (append, once per hour)
  5. Saves the next 3 daily forecasts        -> data/daily_forecasts.csv (append, once per hour)
  6. Logs the run                            -> data/runs.csv           (append)

Standard library only, so the job starts fast and has nothing to install.
"""

import csv
import datetime as dt
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

LAT, LON = 4.3600, 100.9700  # Seri Iskandar, Perak
TZ = "Asia/Kuala_Lumpur"
MAX_HORIZON_H = 72

HOURLY = ["temperature_2m", "relative_humidity_2m", "precipitation",
          "precipitation_probability", "wind_speed_10m"]
CURRENT = ["temperature_2m", "relative_humidity_2m", "precipitation",
           "wind_speed_10m", "weather_code"]
DAILY = ["temperature_2m_max", "temperature_2m_min", "precipitation_sum",
         "precipitation_probability_max"]

FILES = {
    "current": ["fetched_at_utc", "time", "temperature_c", "humidity_pct",
                "precipitation_mm", "wind_kmh", "weather_code"],
    "observations": ["time", "temperature_c", "humidity_pct", "precipitation_mm",
                     "wind_kmh", "updated_at_utc"],
    "forecasts": ["issued_at", "target_time", "horizon_h", "temperature_c",
                  "humidity_pct", "precipitation_mm", "precip_prob_pct", "wind_kmh"],
    "daily_forecasts": ["issued_at", "target_date", "lead_days", "tmax_c", "tmin_c",
                        "precip_sum_mm", "precip_prob_max_pct"],
    "runs": ["run_at_utc", "issued_at", "status", "forecast_rows", "note"],
}


def api_url():
    params = {
        "latitude": LAT,
        "longitude": LON,
        "timezone": TZ,
        "current": ",".join(CURRENT),
        "hourly": ",".join(HOURLY),
        "daily": ",".join(DAILY),
        "past_days": 1,
        "forecast_days": 4,
        "wind_speed_unit": "kmh",
    }
    return "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)


def fetch(retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(api_url(), headers={"User-Agent": "TFB2093-weather-iot"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # network error, HTTP error, bad JSON
            last = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Open-Meteo request failed after {retries} tries: {last}")


# ---------- CSV helpers ----------

def path(name):
    return DATA / f"{name}.csv"


def read_rows(name):
    p = path(name)
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def append_rows(name, rows):
    p = path(name)
    new = not p.exists()
    with p.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FILES[name])
        if new:
            w.writeheader()
        w.writerows(rows)


def write_rows(name, rows):
    with path(name).open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FILES[name])
        w.writeheader()
        w.writerows(rows)


def fmt(v):
    return "" if v is None else v


# ---------- processing ----------

def parse_local(s):
    """Open-Meteo returns local times like '2026-10-17T10:15'."""
    return dt.datetime.fromisoformat(s)


def process(payload, run_at_utc):
    """Turn one API response into CSV rows. Pure function apart from file I/O."""
    DATA.mkdir(exist_ok=True)
    stamp = run_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    cur = payload["current"]
    now_local = parse_local(cur["time"])
    issue_hour = now_local.replace(minute=0, second=0, microsecond=0)
    issued_at = issue_hour.strftime("%Y-%m-%dT%H:%M")

    # 1. current conditions
    append_rows("current", [{
        "fetched_at_utc": stamp,
        "time": cur["time"],
        "temperature_c": fmt(cur.get("temperature_2m")),
        "humidity_pct": fmt(cur.get("relative_humidity_2m")),
        "precipitation_mm": fmt(cur.get("precipitation")),
        "wind_kmh": fmt(cur.get("wind_speed_10m")),
        "weather_code": fmt(cur.get("weather_code")),
    }])

    # 2. split hourly series into actual (past) and forecast (future)
    h = payload["hourly"]
    obs = {r["time"]: r for r in read_rows("observations")}
    fc_rows = []
    for i, t in enumerate(h["time"]):
        t_dt = parse_local(t)
        vals = {k: h[k][i] for k in HOURLY}
        if t_dt <= issue_hour:
            if vals["temperature_2m"] is None:
                continue
            obs[t] = {
                "time": t,
                "temperature_c": fmt(vals["temperature_2m"]),
                "humidity_pct": fmt(vals["relative_humidity_2m"]),
                "precipitation_mm": fmt(vals["precipitation"]),
                "wind_kmh": fmt(vals["wind_speed_10m"]),
                "updated_at_utc": stamp,
            }
        else:
            horizon = int((t_dt - issue_hour).total_seconds() // 3600)
            if 1 <= horizon <= MAX_HORIZON_H:
                fc_rows.append({
                    "issued_at": issued_at,
                    "target_time": t,
                    "horizon_h": horizon,
                    "temperature_c": fmt(vals["temperature_2m"]),
                    "humidity_pct": fmt(vals["relative_humidity_2m"]),
                    "precipitation_mm": fmt(vals["precipitation"]),
                    "precip_prob_pct": fmt(vals["precipitation_probability"]),
                    "wind_kmh": fmt(vals["wind_speed_10m"]),
                })
    write_rows("observations", sorted(obs.values(), key=lambda r: r["time"]))

    # 3. forecasts: store once per issue hour (runs every 30 min, so skip duplicates)
    already = {r["issued_at"] for r in read_rows("runs") if r["status"] == "ok" and int(r["forecast_rows"] or 0) > 0}
    note = ""
    if issued_at in already:
        fc_rows = []
        note = "forecast for this hour already stored"
    else:
        append_rows("forecasts", fc_rows)
        d = payload["daily"]
        today = issue_hour.date()
        drows = []
        for i, day in enumerate(d["time"]):
            lead = (dt.date.fromisoformat(day) - today).days
            if 0 <= lead <= 3:
                drows.append({
                    "issued_at": issued_at,
                    "target_date": day,
                    "lead_days": lead,
                    "tmax_c": fmt(d["temperature_2m_max"][i]),
                    "tmin_c": fmt(d["temperature_2m_min"][i]),
                    "precip_sum_mm": fmt(d["precipitation_sum"][i]),
                    "precip_prob_max_pct": fmt(d["precipitation_probability_max"][i]),
                })
        append_rows("daily_forecasts", drows)

    append_rows("runs", [{"run_at_utc": stamp, "issued_at": issued_at, "status": "ok",
                          "forecast_rows": len(fc_rows), "note": note}])
    return len(fc_rows)


def main():
    run_at = dt.datetime.now(dt.timezone.utc)
    try:
        payload = fetch()
        n = process(payload, run_at)
        print(f"OK: stored {n} forecast rows")
    except Exception as e:
        DATA.mkdir(exist_ok=True)
        append_rows("runs", [{"run_at_utc": run_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                              "issued_at": "", "status": "error", "forecast_rows": 0,
                              "note": str(e)[:200]}])
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
