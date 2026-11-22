# Seri Iskandar Weather Monitor

**TFB2093 Internet of Things project, Universiti Teknologi PETRONAS**

A low-cost IoT data acquisition and analytics system that monitors the weather in
Seri Iskandar, Perak (4.36°N, 100.97°E). It collects Open-Meteo observations and
forecasts on a schedule, stores the full history, measures how accurate the
forecasts are, and shows everything on a live dashboard. A standalone ESP32
station warns people on site when rain is coming.

**Live dashboard:** https://farah-aleya.github.io/seri-iskandar-weather/
**Cloud running cost:** RM 0  |  **Hardware cost:** about RM 85

## Team

| Name | Student ID |
|---|---|
| Nur Farah Aleya binti Mohamad Noor 'Azlan | 24003489 |
| Siti Zafirah binti Zailan | 24003514 |
| Puteri Mia Syazza binti Shahril Ridzwan | 24002615 |
| Nur Ain Nabilah binti Abdullah | 22011754 |

## How it works

```
GitHub Actions (scheduled every 30 min)
   │  collector/collect.py  ── HTTPS ──>  Open-Meteo API
   ▼
data/*.csv  (committed to the repo = permanent history)
   │  analysis/build.py  (match forecast vs actual, compute accuracy)
   ▼
site/data.json + site/weather.db  ──>  GitHub Pages dashboard (site/index.html)

ESP32 rain-alert station ── Wi-Fi/HTTPS ──> Open-Meteo (next 3 h forecast)
   + BME280 + raindrop sensor ──> OLED, traffic light LED, buzzer
```

| Path | What it does |
|---|---|
| `collector/collect.py` | Calls Open-Meteo once per run. Saves current conditions, the past day as actual hourly values, the next 72 hourly forecasts and the next 3 daily forecasts. |
| `analysis/build.py` | Matches every forecast with the actual value for the same hour. Computes MAE, RMSE and bias per variable and horizon, rain POD, FAR, CSI and Brier score, a ±1 hour rain timing tolerance, and a next-day forecast check. Builds `data.json` and `weather.db`. |
| `site/index.html` | Interactive dashboard (Chart.js) with 4 pages: Weather, Accuracy, Rain check, Data. |
| `hardware/` | ESP32 rain-alert station: code, wiring guide and diagrams. See [hardware/README.md](hardware/README.md). |
| `.github/workflows/collect.yml` | Schedule that runs the collector, builds the data and deploys the dashboard. |

## Dashboard

| Page | Contents |
|---|---|
| Weather | Current conditions, rain alert banner, next 24 h every 3 h, 4-day forecast, air conditions, 24 h chart |
| Accuracy | Forecast vs actual chart (choose variable, horizon 1/6/24/48/72 h and time range), MAE/RMSE/bias, error vs lead time, auto-generated key findings, accuracy table |
| Rain check | Rain hit/miss matrix, POD/FAR/CSI/Brier by horizon, exact hour vs ±1 hour comparison, next-day forecast check |
| Data | Collection statistics, daily summary, recent runs, downloads of `weather.db` and the CSV files |

## Database

Raw records are stored as CSV files in `data/` (version-controlled, nothing is lost if a run fails). On every run they are rebuilt into a SQLite database, `weather.db`, which can be downloaded from the dashboard.

| Table | Primary key | Contents |
|---|---|---|
| `observations` | `time` | Actual hourly temperature (°C), humidity (%), rainfall (mm), wind speed (km/h) |
| `forecasts` | `issued_at` + `target_time` | Hourly forecast values, horizon (1 to 72 h), chance of rain |
| `daily_forecasts` | `issued_at` + `target_date` | Daily max/min temperature, rain total, max chance of rain, 0 to 3 days ahead |
| `current_readings` | `fetched_at_utc` | Current conditions at each run |
| `runs` | `run_at_utc` | Log of every run (ok or error) |

The view `forecast_vs_actual` joins `forecasts` and `observations` on `target_time = time` and gives the error (forecast minus actual) for each variable.

## Data collection

- Collection started on **17 October 2026**.
- GitHub runs free scheduled workflows on a best-effort basis, so runs came about every 4 hours instead of every 30 minutes.
- Each run re-downloads the past day of actual values, so the hourly record stays **100% complete** even when a run is late or fails.
- Each forecast snapshot covers the next 72 hours, so every hour is forecast many times at different lead times.

## Key findings

- **Temperature** was the most reliable variable: error about 1 °C at every lead time, with the forecast usually slightly too cool.
- **Humidity** forecasts were slightly too high and **wind** slightly too low.
- **Rain** was the hardest to forecast at the exact hour. Allowing ±1 hour raised the rain skill (CSI) from about 0.50 to about 0.71, so most rain errors were timing errors in short tropical showers.
- Rain forecasts got worse with lead time, so forecasts made less than 12 hours ahead are the most useful.

Live, up-to-date figures are on the dashboard's Accuracy and Rain check pages.

## Setup

1. Use a **public** GitHub repository (free Actions minutes and free Pages).
2. Upload all files, keeping the folder structure (including `.github/workflows/`).
3. **Settings → Pages → Source: GitHub Actions**.
4. **Settings → Actions → General → Workflow permissions: Read and write**.
5. **Actions → Collect weather → Run workflow**.
6. When the run is green, open `https://<username>.github.io/<repo-name>/`.

To run locally:

```
python collector/collect.py
python analysis/build.py
cd site && python -m http.server
```

## Limitations

- "Actual" values are Open-Meteo model analysis values for the location, not a physical rain gauge or weather station.
- GitHub's free scheduler delays runs, so forecast snapshots are taken every few hours instead of every 30 minutes.
- The low-cost BME280 sensor occasionally needs a restart if a jumper wire is loose.

## Credits

Weather data by [Open-Meteo.com](https://open-meteo.com/), licensed CC BY 4.0.
