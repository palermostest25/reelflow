# Multicam engine (vendored)

These modules are the editing engine from Multicam Studio
(`palermostest25/multicam-studio`, commit `2505ceea9f1ba2a70a664bca9b5a6051e8abb1de`), copied with one change (the final verification error lists the durations it found):
`multicam_edit.py`, `planner.py`, `music.py`, `vision.py`, `looks.py` and `hardware.py`.

Reelflow runs `multicam_edit.py` as a separate process (see `app/multicam.py`), so it keeps its own
imports and command line. To update, copy the same files from a newer Multicam Studio commit, update
the commit above, and run `python -m pytest tests/test_multicam_engine.py`.
