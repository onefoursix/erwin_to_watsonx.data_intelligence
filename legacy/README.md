# legacy: the single erwin-wxdi-import skill

Replaced on 2026-11-03 by the three skills in `../skills/` (erwin-model-export, wxdi-metadata-import,
erwin-wxdi-governance). Kept for reference: `scripts/wxdi_import.py` has steps the new skills don't carry over
(`descriptions`, `teardown`, the Postgres-only `run-all`). It still expects `erwin2postgres.py` next to it,
which now lives in `../shared/`. Delete this folder once nothing here is needed.
