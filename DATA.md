# DATA: sources, laws and dates

_Translated and condensed from the author's document of 2026-10-14 (`docs/sources_et_methodes_FR.txt`, French)._

## Key facts

- **All five datasets are simulated** by Python scripts (numpy, random seed 42). **No real observation is used.**
- **Nothing was downloaded from an external source**, so there is no retrieval date. The CSV files were generated on
  **2026-09-28**.
- CHIRPS, ERA5-Land, Sentinel-2, SoilGrids and SIM-Agricole appear in the challenge brief as *target* sources only;
  none is used.
- The origin of the simulation parameters (mean rainfall per department, temperatures, base prices, seasonality) is
  not documented and cannot be attributed to those sources.
- Results are prototype estimates, not validated in the field.

Common scope: 12 departments of Benin (Alibori, Atacora, Atlantique, Borgou, Collines, Couffo, Donga, Littoral, Mono,
Oueme, Plateau, Zou).

## Files

| File | Rows | Used by the engine | Generation |
|---|---|---|---|
| `dataset_M1_pluie_saisonniere.csv` | 528 (12 x 44 years, 1981-2024) | SST, ENSO, humidity, previous-season rain, department -> season rain | Rain ~ Normal(mean, sd of the department) x (1 + 0.08 sst) x (1 - 0.06 enso) x (1 + 0.005 (humidity - 45)); previous rain is random (no signal) |
| `dataset_M2_scenarios_journaliers.csv` | 288,000 | `pluie_journaliere_mm` daily rain profiles. Not used: `probabilite_scenario` (random) | 35% rainy days, Gamma intensities, rescaled to the target total; no calendar seasonality |
| `dataset_M3_etc_fao56.csv` | 35,040 | `temp_max`, `temp_min`, `rayonnement` -> mean ET0 per department. Not used: `et0_mm` (about 8 mm/day, too high), `kc`, `etc`, humidity, wind | Temperature and radiation drawn by zone (north/south), no seasonal variation |
| `dataset_M4_prix_marche.csv` | 21,600 | `prix_moyen` (FCFA/kg) for maize, rice, yam, cassava, tomato | Base price x seasonality x (1 + 0.02 (year - 2015)) x Normal(1; 0.08) noise, bounded to 60-150% of base |
| `dataset_M5_irrigation.csv` | 28,800 | Not used by the application | The irrigation plan comes from the water balance, not from this file |

`data/households.sqlite` is a separate **synthetic** register (`python -m agri_predict.synth_households`, seed 7).

## Methods (summary)

- **L1** Season rainfall: Ridge regression (alpha = 1) on 4 climate variables + department.
- **L2** Three scenarios: total = M1 forecast + z x sigma, z = -1, 0, +1, probabilities 25/50/25%; each total spread over
  120 days with a historical daily profile (M2).
- **L3** ET0, Hargreaves-Samani (radiation form): ET0 = 0.0135 x (Tmean + 17.8) x Rs x 0.408 (Rs in MJ/m2/day), clipped to
  2-10 mm/day; about 5.0 mm/day north, 4.3 south. This is the "corrected ET0" version of the engine.
- **L4** Crop need (FAO-56): ETc = ET0 x Kc; Kc constant initial, linear development, constant mid-season, linear late.
- **L5** Effective rain (USDA-SCS simplification): 0.8 P if P <= 75 mm, else 0.3 P + 37.5.
- **L6** Daily soil water balance: field capacity 100 mm, wilting point 30 mm, irrigate when deficit > 20 mm, maximum
  30 mm/day.
- **L7** Volume: 1 mm over 1 ha = 10 m3.
- **L8** Prices: linear regression (trend + month effect) on 2015-2024, extrapolated from January 2025; range =
  forecast +/- 1.28 x residual sd.
- **L9** Harvest date = sowing + cycle length (maize 98 days, rice 115, tomato 86, yam 206, cassava 342); storage advice
  compares the harvest-month price with the best price in the window.
- Storage maximum (maize and rice 6 months, yam 4, cassava and tomato 0) and Kc values are prototype assumptions to be
  validated with an agronomist; the yam Kc in particular.

## Validation (on simulated data, so it proves nothing about real accuracy)

- M1: train 1981-2014, test 2015-2024. Mean error 116 mm vs 134 mm for the local mean; R2 = 0.69.
- Prices: last 24 months, 60 series. Mean error 28.7 FCFA/kg (SARIMA: 34.4); the range contains the true value about
  70% of the time.

## Known limits

No calendar rainy season in the data (a dry-season sowing still receives a "normal season"; see EVALS A18 and
`sowing_windows.py` for the only checked case); prices known until December 2024 so 2027-2028 forecasts are far
extrapolations; irrigation plan limited to 120 days (yam and cassava have longer cycles); same rainy-day pattern for
the three scenarios, so little difference between dry and wet irrigation need.

## To move to real data

Rain: CHIRPS. Temperature, radiation, wind, humidity: ERA5-Land. Prices: SIM-Agricole. Yields: INStaD. For each source,
record the URL, version, period and download date here.
