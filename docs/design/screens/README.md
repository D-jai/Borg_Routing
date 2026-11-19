# Screens

The screenshots of the pages are not made by hand and are not stored here by the check. They
come from the CI job `ui_check` (`.github/workflows/ci.yml`, work order 4.6), which runs
`scripts/check_ui.py` against the app in replay mode.

To get them: open the CI run on GitHub, then download the artifact `ui-check`. It holds:

- `ui_check_out/<page>_<width>_<theme>.png`: each of the ten pages at 390 and 1280 px wide,
  light and dark (40 images);
- `ui_check_out/ask_played_1280_light.png` and `run_played_1280_light.png`: the Ask page after
  playing recorded job 1 to the end, and the Run page of that playback;
- `ui_check_out/report.json`: every check, pass or fail, with its reasons;
- `uvicorn.log`: the replay server's log.

The check never runs on the maintainer's laptop (planner ruling 38a). A screenshot that the
README or the deck uses is copied into this folder by hand from a passing run's artifact, with
the run named in the commit message.
