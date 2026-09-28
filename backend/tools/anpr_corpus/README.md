# ANPR benchmark corpus

Empty on purpose. `tools/anpr_bench.py` needs real, labelled license-plate
images to measure anything — a synthetic or placeholder set would produce a
number that looks like evidence while measuring nothing, which is worse than
no number at all. See that tool's own docstring for the full reasoning.

## Why this is not filled in for you

Sourcing real plate photos here would mean either:

- **scraping/downloading real vehicles' plates from the internet** — a
  privacy and likely copyright problem, and the opposite of the "never
  publish real data" stance the rest of this codebase takes (see the evidence
  chain-of-custody and Sentinel Grid credential handling); or
- **generating synthetic plate images** — measures the renderer, not real-world
  OCR accuracy, and the benchmark tool explicitly refuses to ship one for
  exactly this reason.

Neither is an honest substitute for real CCTV-quality footage of real
Gujarat-format plates. This has to come from you or an authorized source
(recorded test footage, a licensed dataset, or frames pulled from the actual
Sentinel Grid cameras with permission) — not fabricated here.

## What to drop in here

- 50-100 real images, one plate each — full frames or vehicle crops both work
  (the harness runs the same localization step the live pipeline uses).
- Filename = the ground-truth plate, uppercase, format tolerant of a
  `_suffix` for your own notes:

  ```text
  GJ05AB1234.jpg
  GJ01XY7788_night.jpg
  GJ18CD4455_blur.jpg
  GJ27EF9012_angled.jpg
  ```

- Cover the conditions that matter operationally, not just clean daylight
  shots: night/low-light, motion blur, oblique viewing angle, partial
  occlusion, and real Gujarat plate fonts/spacing.

## Running it

```bash
cd backend
.venv/Scripts/python.exe tools/anpr_bench.py tools/anpr_corpus --json anpr_results.json
```

Reports exact-match rate, plausible-format rate, character error rate, mean
confidence, and mean seconds/image — for whole-crop OCR, localized OCR, and
(if installed) a PaddleOCR candidate. No verdict is printed; the tool's
docstring explains why that decision is yours to make from the numbers.

## `.gitignore`

Real plate images do not belong in version control (same reasoning as
`backend/uploads/` and `backend/evidence_store/`). This directory's contents
beyond this README are ignored — see the root `.gitignore`.
