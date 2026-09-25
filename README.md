# Dead-Air Remover — macOS Intel build

Removes dead-air/silence from a video, frame-tolerance based, for a
CapCut export -> clean -> re-import workflow.

## What's actually been verified so far, and how

I do not have access to any macOS machine (Intel or Apple Silicon), and this
build environment has no network access and cannot cross-compile a macOS
`.app` — PyInstaller packages whatever OS it runs *on*, not a target you
name. So here's the honest split of what's tested vs. what isn't yet:

**Verified (headlessly, on Linux, using the same code the app ships):**
- `app/engine.py` — ffprobe parsing, `silencedetect` parsing, frame math,
  cut computation (min-silence-frames, keep-before/after padding, merging,
  clamping) — tested against synthetic ffmpeg-generated clips with known
  frame-exact silence lengths, including a sub-threshold gap that must be
  correctly *ignored*, and a fractional-NTSC (30000/1001 fps) clip.
- `app/cutter.py` — the single-pass `select`/`aselect` ffmpeg cut, checked
  for exact output frame counts, no residual gaps, and A/V sync
  (`-avoid_negative_ts make_zero` applied after an initial run surfaced a
  negative first-audio-PTS artifact).
- `app/selftest.py` — packages all of the above into one script
  (`--selftest`) that generates a synthetic clip, runs the full pipeline,
  and asserts exact frame counts. **This exact script is what CI runs
  against the actual built .app** (see below) — it's not a different,
  looser check.

**NOT yet verified by me (because I have no way to):**
- That `DeadAirRemover.app` actually launches and runs on real Intel
  macOS hardware.
- The GUI code (`app/gui.py`) — it's syntax-checked and carefully
  reviewed, and it's a thin, direct wrapper around the already-verified
  engine/cutter (same function calls, same objects), but it has never
  been executed, because this sandbox has no tkinter and no display.
- The ffmpeg download URLs in `build_macos.sh` (evermeet.cx) — I can't
  reach the network to confirm they're still live/unchanged.

## Getting a tested .app without touching a Mac yourself

Push this folder to a GitHub repo and either push to a branch or manually
run the included workflow:

1. Create a new (can be private) GitHub repo, push this folder to it.
2. Go to the repo's **Actions** tab → **Build & Test macOS Intel App** →
   **Run workflow** (or just push — it also runs on every push).
3. GitHub runs this on `macos-13`, their last genuinely Intel (x86_64)
   runner generation (pinned deliberately — later runners are Apple
   Silicon). The job:
   - downloads static Intel ffmpeg/ffprobe
   - builds `DeadAirRemover.app` as a forced `x86_64` PyInstaller bundle
   - runs `DeadAirRemover.app/Contents/MacOS/DeadAirRemover --selftest`
     — the *actual packaged binary*, using its *own bundled ffmpeg* —
     and fails the build if any assertion fails
   - separately launches the app normally (`open`) and confirms it's
     still running 5 seconds later, as a basic GUI-doesn't-crash-on-launch
     smoke test
   - zips and uploads the result as a build artifact
4. Once the run is green, download **DeadAirRemover-macOS-Intel.zip**
   from the run's Artifacts section.

This gets you a real, tested Intel binary without needing your own Mac —
the only step you have to do yourself is the GitHub push/click, since I
have no way to push code or trigger CI on your behalf from here.

## If you (or someone) ever do have an Intel Mac, or an Apple Silicon Mac with Rosetta 2

```bash
chmod +x build_macos.sh
./build_macos.sh
```

This does the same thing as the CI job, locally, including the
`--selftest` verification gate, and tells you exactly what to do at the
end (first launch needs right-click → Open once, since the app is
ad-hoc signed but not notarized by Apple).

## Using the app

1. Open `DeadAirRemover.app` (first time: right-click → Open, per above).
2. Drag & drop your exported CapCut MP4 in (or Browse...).
3. Pick a preset (**Very Tight** for fast-paced Shorts/Reels/TikTok) or
   dial in Minimum Silence (frames), Silence Threshold (dB), Keep
   Before/After Speech (frames) yourself — every value is manually
   overridable regardless of preset.
4. Click **ANALYZE**. Review the detected cuts list and the waveform
   preview; double-click (or click the checkbox column of) any row to
   exclude that one cut from removal.
5. Click **REMOVE SILENCES**, pick where to save the output. Your
   original file is never touched — you're always asked for a new
   output path.
6. Import the `_NO_SILENCE.mp4` result back into CapCut.

## Known, inherent limits (not bugs)

- Audio cuts are quantized to AAC's fixed 1024-sample frame grid
  (~21ms @ 48kHz) — universal to any AAC-based cutting tool, imperceptible
  in practice, and why very small `Keep Before/After` values can end up a
  few ms larger than the exact frame math suggests.
- The waveform preview draws at ffmpeg's native `showwavespic` render
  size rather than dynamically rescaling to the window — resize-aware
  scaling would need Pillow, which was left out to keep the bundle small
  and dependency-free.
