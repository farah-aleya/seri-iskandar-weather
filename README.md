# Seri Iskandar Weather Monitor (TFB2093 IoT Project)

A low-cost IoT data acquisition and analytics system that monitors the weather in
Seri Iskandar, Perak. It collects Open-Meteo observations and forecasts every 30
minutes, stores the history, measures forecast accuracy, and shows everything on a
live dashboard. Running cost: RM 0.

## How it works

```
GitHub Actions (every 30 min)
   │  collector/collect.py  ── HTTPS ──>  Open-Meteo API
   ▼
data/*.csv  (committed to the repo = permanent history)
   │  analysis/build.py
   ▼
site/data.json + site/weather.db  ──>  GitHub Pages dashboard (site/index.html)

ESP32 rain-alert device ── HTTPS (every 10 min) ──> Open-Meteo API ──> green/yellow/red LED + buzzer
```

| Folder | What it does |
|---|---|
| `collector/collect.py` | Calls Open-Meteo once per run. Stores current conditions, past hours as actual values, the next 72 hourly forecasts and the next 3 daily forecasts. |
| `analysis/build.py` | Matches every forecast with the actual value for the same hour. Computes MAE, RMSE, bias per variable and horizon, plus rain POD, FAR, CSI and Brier score. Builds `data.json` and `weather.db`. |
| `site/index.html` | The dashboard (Chart.js). |
| `hardware/rain_alert/` | ESP32 sketch for the bonus rain-alert device. |
| `.github/workflows/collect.yml` | The schedule that runs everything every 30 minutes. |

## Data files

| File | Key | Contents |
|---|---|---|
| `observations.csv` | `time` (hour) | Actual temperature, humidity, rainfall, wind for each hour |
| `forecasts.csv` | `issued_at` + `target_time` | Each hourly forecast, its horizon (1 to 72 h), and the chance of rain |
| `daily_forecasts.csv` | `issued_at` + `target_date` | Daily max/min temperature, rain total, chance of rain, lead 0 to 3 days |
| `current.csv` | `fetched_at_utc` | Current conditions at each run |
| `runs.csv` | `run_at_utc` | Log of every run (ok or error) |

`weather.db` holds the same tables in SQLite plus a `forecast_vs_actual` view.

## Setup (about 15 minutes)

1. Create a **public** GitHub repository (public = free unlimited Actions minutes and free Pages).
2. Upload all files in this folder, keeping the folder structure (including `.github/workflows/`).
3. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. Repo **Settings → Actions → General → Workflow permissions: Read and write permissions** → Save.
5. **Actions** tab → enable workflows if asked → **Collect weather → Run workflow**.
6. When the run is green, open `https://<your-username>.github.io/<repo-name>/`. That URL is your deployment link.
7. Leave it running for at least 3 full days (4 to 5 is safer).

## Run locally (optional)

```
python collector/collect.py
python analysis/build.py
cd site && python -m http.server
```

## Notes and limitations

- Open-Meteo "actual" values are model analysis values for the location, not a physical weather station.
- GitHub may delay scheduled runs by a few minutes; running every 30 minutes keeps at least one forecast per hour.
- Weather data by [Open-Meteo.com](https://open-meteo.com/), CC BY 4.0.
