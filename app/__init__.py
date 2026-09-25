
"""
cutter.py
Turns a list of "keep" segments into a single ffmpeg invocation that
produces the final, gap-free video.

Approach:
    We do this in ONE ffmpeg pass using the `select` / `aselect` filters
    rather than the concat demuxer. This avoids:
      - writing dozens of intermediate segment files
      - keyframe-alignment problems that make `-c copy` trimming inaccurate
      - extra re-encode generations (concat-of-re-encoded-segments would
        re-encode twice)

    Video frames are selected by FRAME NUMBER (`between(n,f1,f2)`), which is
    exact - no floating point drift. Audio is selected by TIME
    (`between(t,t1,t2)`) using the same segment boundaries converted back to
    seconds via frame/fps, which keeps it aligned with the chosen video
    frames. `setpts`/`asetpts` re-time both streams so there are no gaps.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from fractions import Fraction
from typing import List, Optional, Tuple

from .engine import MediaInfo, seconds_to_frames, ffmpeg_bin


@dataclass
class CutPlan:
    """Precomputed, ready-to-run ffmpeg command for a cut job."""
    input_path: str
    output_path: str
    select_expr: str
    aselect_expr: str
    has_audio: bool
    fps: Fraction
    keep_segment_count: int
    kept_frame_count: int

    @property
    def fps_str(self) -> str:
        """Exact fps as 'num/den' for ffmpeg's -r, avoiding decimal rounding
        drift on fractional rates like NTSC's 30000/1001."""
        return f"{self.fps.numerator}/{self.fps.denominator}"


def build_cut_plan(
    media: MediaInfo,
    keep_segments: List[Tuple[float, float]],
    output_path: str,
) -> CutPlan:
    if not keep_segments:
        raise ValueError("No segments left to keep - every frame would be cut.")

    fps = media.fps
    frame_ranges: List[Tuple[int, int]] = []
    for start, end in keep_segments:
        f_start = seconds_to_frames(start, fps)
        f_end = seconds_to_frames(end, fps) - 1  # inclusive end frame
        if f_end < f_start:
            f_end = f_start
        frame_ranges.append((f_start, f_end))

    # Merge any ranges that became adjacent/overlapping after rounding.
    frame_ranges.sort()
    merged: List[Tuple[int, int]] = []
    for fs, fe in frame_ranges:
        if merged and fs <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], fe))
        else:
            merged.append((fs, fe))

    select_terms = [f"between(n\\,{fs}\\,{fe})" for fs, fe in merged]
    select_expr = "+".join(select_terms)

    # Audio uses time-based selection with the exact same boundaries
    # (converted back from frame numbers) so it lines up with the chosen
    # video frames rather than drifting from independent silence timestamps.
    aselect_terms = []
    for fs, fe in merged:
        t_start = fs / float(fps)
        t_end = (fe + 1) / float(fps)
        aselect_terms.append(f"between(t\\,{t_start:.6f}\\,{t_end:.6f})")
    aselect_expr = "+".join(aselect_terms)

    kept_frames = sum(fe - fs + 1 for fs, fe in merged)

    return CutPlan(
        input_path=media.path,
        output_path=output_path,
        select_expr=select_expr,
        aselect_expr=aselect_expr,
        has_audio=media.has_audio,
        fps=fps,
        keep_segment_count=len(merged),
        kept_frame_count=kept_frames,
    )


def run_cut(
    plan: CutPlan,
    crf: int = 16,
    preset: str = "slow",
    progress_cb: Optional[callable] = None,
) -> subprocess.CompletedProcess:
    """
    Executes the ffmpeg command described by `plan`. Raises
    subprocess.CalledProcessError on failure (with stderr captured on the
    exception via `.stderr` if you set capture_output).
    """
    if plan.has_audio:
        filter_complex = (
            f"[0:v]select='{plan.select_expr}',setpts=N/FRAME_RATE/TB[v];"
            f"[0:a]aselect='{plan.aselect_expr}',asetpts=N/SR/TB[a]"
        )
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        filter_complex = f"[0:v]select='{plan.select_expr}',setpts=N/FRAME_RATE/TB[v]"
        maps = ["-map", "[v]"]

    cmd = [
        ffmpeg_bin(), "-y", "-nostdin", "-hide_banner",
        "-i", plan.input_path,
        "-filter_complex", filter_complex,
        *maps,
        "-r", plan.fps_str,
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-pix_fmt", "yuv420p",
    ]
    if plan.has_audio:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    # Prevents negative PTS on the first audio packet (a side-effect of
    # AAC encoder priming samples) which can otherwise show up as a tiny
    # A/V sync offset in some players/editors.
    cmd += ["-avoid_negative_ts", "make_zero"]
    cmd += [plan.output_path]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg cut failed:\n{proc.stderr[-4000:]}")
    return proc

"""
engine.py
Core, GUI-free logic for the Dead-Air Remover tool.

Responsibilities:
- Probe a media file for resolution / fps / duration / sample rate.
- Run ffmpeg's `silencedetect` filter once per (threshold) to get RAW
  silence intervals (using the smallest possible minimum duration, so we
  capture every silence and can re-filter by frame-count entirely in
  Python without re-invoking ffmpeg every time the user nudges a slider).
- Convert user-facing FRAME settings into time using the video's real FPS.
- Turn raw silence intervals + user settings into a final list of
  "cut" (remove) regions and the complementary "keep" regions.

All time values are in seconds (floats) unless a name ends in `_frame`
or `_frames`, in which case it is an integer frame count.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from typing import List, Optional, Tuple


# --------------------------------------------------------------------------- #
# Locating ffmpeg/ffprobe, whether running from source or from a PyInstaller
# .app bundle that carries its own copies.
# --------------------------------------------------------------------------- #

def _bundled_binary_dir() -> Optional[str]:
    """Directory containing bundled ffmpeg/ffprobe when frozen by PyInstaller
    (see build_macos.sh, which places them under Resources/ffmpeg-bin next to
    the executable, and the .spec file, which copies that folder into the
    app bundle's `sys._MEIPASS` at runtime for a onefile build, or next to
    the executable for a onedir build)."""
    if getattr(sys, "frozen", False):
        # onefile: extracted temp dir; onedir: the app's own folder.
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        candidate = os.path.join(base, "ffmpeg-bin")
        if os.path.isdir(candidate):
            return candidate
    return None


def _binary_path(name: str) -> str:
    """Returns the bundled binary path if present, else just the bare name
    (resolved via PATH, e.g. during development or if the user has ffmpeg
    installed system-wide)."""
    d = _bundled_binary_dir()
    if d:
        candidate = os.path.join(d, name)
        if os.path.isfile(candidate):
            return candidate
    return name


def ffmpeg_bin() -> str:
    return _binary_path("ffmpeg")


def ffprobe_bin() -> str:
    return _binary_path("ffprobe")


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class MediaInfo:
    path: str
    width: int
    height: int
    fps: Fraction                # exact fps, e.g. Fraction(30000, 1001)
    duration: float               # seconds, from container/format
    sample_rate: int              # audio sample rate, Hz (0 if no audio stream)
    has_audio: bool
    nb_frames_hint: Optional[int] # best-effort frame count, may be None

    @property
    def fps_float(self) -> float:
        return float(self.fps)


@dataclass
class SilenceInterval:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class CutSegment:
    """A region of the timeline that will be REMOVED."""
    start: float
    end: float
    source_silence: SilenceInterval  # the raw silence this cut came from
    enabled: bool = True             # user can disable individual cuts

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class AnalysisResult:
    media: MediaInfo
    raw_silences: List[SilenceInterval]
    cuts: List[CutSegment]
    settings: "CutSettings"

    @property
    def total_removed(self) -> float:
        return sum(c.duration for c in self.cuts if c.enabled)

    @property
    def output_duration(self) -> float:
        return max(0.0, self.media.duration - self.total_removed)


@dataclass
class CutSettings:
    min_silence_frames: int = 4
    threshold_db: float = -40.0
    keep_before_frames: int = 1   # frames of silence preserved right before speech resumes
    keep_after_frames: int = 1    # frames of silence preserved right after speech ends


# --------------------------------------------------------------------------- #
# ffprobe
# --------------------------------------------------------------------------- #

class ProbeError(RuntimeError):
    pass


def probe_media(path: str) -> MediaInfo:
    """Run ffprobe and extract everything we need. Raises ProbeError on failure."""
    cmd = [
        ffprobe_bin(), "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as e:
        raise ProbeError(
            "ffprobe was not found (neither bundled nor on PATH). If you're "
            "running from source, install FFmpeg; the packaged .app should "
            "never hit this."
        ) from e
    except subprocess.TimeoutExpired as e:
        raise ProbeError("ffprobe timed out while probing the file.") from e

    if proc.returncode != 0:
        raise ProbeError(f"ffprobe failed:\n{proc.stderr.strip()}")

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ProbeError(f"Could not parse ffprobe output: {e}") from e

    streams = data.get("streams", [])
    fmt = data.get("format", {})

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video_stream is None:
        raise ProbeError("No video stream found in file.")

    # fps: prefer r_frame_rate, fall back to avg_frame_rate
    fps_str = video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate") or "0/1"
    try:
        num, den = fps_str.split("/")
        fps = Fraction(int(num), int(den)) if int(den) != 0 else Fraction(0)
    except Exception:
        fps = Fraction(30)  # last-ditch fallback, should not normally happen

    if fps <= 0:
        raise ProbeError(f"Could not determine a valid FPS from stream (got '{fps_str}').")

    width = int(video_stream.get("width", 0))
    height = int(video_stream.get("height", 0))

    duration = 0.0
    for candidate in (fmt.get("duration"), video_stream.get("duration")):
        if candidate is not None:
            try:
                duration = float(candidate)
                break
            except ValueError:
                continue

    nb_frames_hint = None
    if video_stream.get("nb_frames"):
        try:
            nb_frames_hint = int(video_stream["nb_frames"])
        except ValueError:
            nb_frames_hint = None

    has_audio = audio_stream is not None
    sample_rate = 0
    if has_audio and audio_stream.get("sample_rate"):
        try:
            sample_rate = int(audio_stream["sample_rate"])
        except ValueError:
            sample_rate = 0

    return MediaInfo(
        path=path,
        width=width,
        height=height,
        fps=fps,
        duration=duration,
        sample_rate=sample_rate,
        has_audio=has_audio,
        nb_frames_hint=nb_frames_hint,
    )


# --------------------------------------------------------------------------- #
# silencedetect
# --------------------------------------------------------------------------- #

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)\s*\|\s*silence_duration:\s*(-?[0-9.]+)")

# Smallest duration we ever ask ffmpeg's silencedetect for. We always pass a
# tiny value here (NOT the user's frame setting) so we capture every silence
# region at this dB threshold once, then filter by frame-count in Python.
_MIN_PROBE_DURATION = 0.02  # seconds


def detect_raw_silences(path: str, threshold_db: float, duration_hint: float = 0.0) -> List[SilenceInterval]:
    """
    Run ffmpeg's silencedetect filter and return every silence interval at
    the given dB threshold, using a very small minimum duration so the
    result is a superset of anything the user could ask for by adjusting
    the frame-count slider afterwards.
    """
    cmd = [
        ffmpeg_bin(), "-nostdin", "-hide_banner", "-i", path,
        "-af", f"silencedetect=noise={threshold_db}dB:d={_MIN_PROBE_DURATION}",
        "-f", "null", "-",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as e:
        raise ProbeError("ffmpeg was not found (neither bundled nor on PATH).") from e
    except subprocess.TimeoutExpired as e:
        raise ProbeError("ffmpeg timed out during silence analysis.") from e

    log = proc.stderr  # silencedetect writes to stderr

    starts = []
    intervals: List[SilenceInterval] = []
    pending_start: Optional[float] = None

    for line in log.splitlines():
        m_start = _SILENCE_START_RE.search(line)
        if m_start:
            pending_start = float(m_start.group(1))
            continue
        m_end = _SILENCE_END_RE.search(line)
        if m_end:
            end_t = float(m_end.group(1))
            if pending_start is not None:
                intervals.append(SilenceInterval(start=max(0.0, pending_start), end=end_t))
                pending_start = None
            else:
                # silence_end with no matching start (can happen if the file
                # starts silent and ffmpeg only logs the end) - reconstruct
                # using reported silence_duration.
                dur = float(m_end.group(2))
                intervals.append(SilenceInterval(start=max(0.0, end_t - dur), end=end_t))

    # A silence that runs to end-of-file has no silence_end line. Handle it.
    if pending_start is not None and duration_hint > pending_start:
        intervals.append(SilenceInterval(start=pending_start, end=duration_hint))

    return intervals


# --------------------------------------------------------------------------- #
# Frame <-> time helpers
# --------------------------------------------------------------------------- #

def frames_to_seconds(frames: int, fps: Fraction) -> float:
    return float(frames) / float(fps)


def seconds_to_frames(seconds: float, fps: Fraction) -> int:
    return int(round(seconds * float(fps)))


# --------------------------------------------------------------------------- #
# Cut computation (pure Python, fast - re-run on every slider tweak)
# --------------------------------------------------------------------------- #

def compute_cuts(
    raw_silences: List[SilenceInterval],
    media: MediaInfo,
    settings: CutSettings,
) -> List[CutSegment]:
    """
    Turn raw silence intervals into actual cut regions, honoring:
      - min_silence_frames: silence must be at least this long (in frames,
        measured on the ORIGINAL untrimmed silence) to qualify at all.
      - keep_after_frames: frames of silence preserved immediately after
        speech ends (i.e. trimmed off the START of the cut region).
      - keep_before_frames: frames of silence preserved immediately before
        speech resumes (i.e. trimmed off the END of the cut region).

    Adjacent/overlapping resulting cuts are merged. Cuts are clamped to
    [0, media.duration].
    """
    fps = media.fps
    min_dur = frames_to_seconds(settings.min_silence_frames, fps)
    keep_after = frames_to_seconds(settings.keep_after_frames, fps)
    keep_before = frames_to_seconds(settings.keep_before_frames, fps)

    raw_cuts: List[CutSegment] = []
    for interval in sorted(raw_silences, key=lambda i: i.start):
        if interval.duration < min_dur - 1e-9:
            continue  # doesn't meet the minimum-silence-length requirement

        trimmed_start = interval.start + keep_after
        trimmed_end = interval.end - keep_before

        # Clamp to file bounds
        trimmed_start = max(0.0, trimmed_start)
        trimmed_end = min(media.duration, trimmed_end)

        if trimmed_end - trimmed_start <= 1e-6:
            continue  # padding ate the whole interval - nothing to cut

        raw_cuts.append(CutSegment(start=trimmed_start, end=trimmed_end, source_silence=interval))

    # Merge overlapping / touching cut regions (can happen when keep_before
    # of one interval and keep_after of the next leave no gap, or when the
    # user sets large keep values).
    merged: List[CutSegment] = []
    for cut in raw_cuts:
        if merged and cut.start <= merged[-1].end + 1e-6:
            prev = merged[-1]
            new_end = max(prev.end, cut.end)
            merged[-1] = CutSegment(
                start=prev.start,
                end=new_end,
                source_silence=prev.source_silence,
                enabled=True,
            )
        else:
            merged.append(cut)

    return merged


def keep_segments_from_cuts(
    cuts: List[CutSegment], media: MediaInfo
) -> List[Tuple[float, float]]:
    """
    Given the (enabled) cut list, return the complementary list of segments
    to KEEP, as (start, end) tuples covering [0, media.duration].
    """
    enabled_cuts = sorted([c for c in cuts if c.enabled], key=lambda c: c.start)
    keep: List[Tuple[float, float]] = []
    cursor = 0.0
    for c in enabled_cuts:
        if c.start > cursor + 1e-9:
            keep.append((cursor, c.start))
        cursor = max(cursor, c.end)
    if media.duration - cursor > 1e-6:
        keep.append((cursor, media.duration))
    return keep


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #

PRESETS = {
    "Natural": CutSettings(min_silence_frames=10, threshold_db=-40.0, keep_before_frames=3, keep_after_frames=3),
    "Tight": CutSettings(min_silence_frames=6, threshold_db=-40.0, keep_before_frames=2, keep_after_frames=2),
    "Very Tight": CutSettings(min_silence_frames=3, threshold_db=-38.0, keep_before_frames=1, keep_after_frames=1),
}

"""
gui.py
Tkinter front-end for the Dead-Air Remover.

Uses only the Python standard library (tkinter/ttk) plus, optionally,
`tkinterdnd2` for real OS-level drag-and-drop (auto-detected; the app works
fine with the Browse button if it isn't installed).
"""

from __future__ import annotations

import os
import queue
import threading
import tempfile
import traceback
from typing import Optional

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from . import engine
from . import cutter
from . import waveform

# --------------------------------------------------------------------------- #
# Optional drag & drop support
# --------------------------------------------------------------------------- #
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _HAS_DND = True
except ImportError:
    _HAS_DND = False


APP_TITLE = "Dead-Air Remover"

FRAME_PRESET_VALUES = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15]
DB_PRESET_VALUES = [-20, -25, -30, -35, -40, -45, -50, -55, -60]


def _fmt_time(t: float) -> str:
    m = int(t // 60)
    s = t - m * 60
    return f"{m:02d}:{s:06.3f}"


class LabeledCombo(ttk.Frame):
    """A label + an editable combobox (dropdown of presets, but free typing
    allowed) sharing a tk.StringVar, with an optional numeric type coercion."""

    def __init__(self, parent, label: str, values, default, width=8, is_float=False):
        super().__init__(parent)
        self.is_float = is_float
        ttk.Label(self, text=label).pack(side="left", padx=(0, 6))
        self.var = tk.StringVar(value=str(default))
        self.combo = ttk.Combobox(
            self, textvariable=self.var, values=[str(v) for v in values],
            width=width,
        )
        self.combo.pack(side="left")

    def get_value(self):
        raw = self.var.get().strip()
        try:
            return float(raw) if self.is_float else int(round(float(raw)))
        except ValueError:
            raise ValueError(f"'{raw}' is not a valid number")

    def set_value(self, v):
        self.var.set(str(v))

    def bind_change(self, fn):
        self.combo.bind("<<ComboboxSelected>>", fn)
        self.combo.bind("<KeyRelease>", fn)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("980x760")
        self.root.minsize(880, 640)

        self.media: Optional[engine.MediaInfo] = None
        self.raw_silences = []
        self.analysis: Optional[engine.AnalysisResult] = None
        self.waveform_png: Optional[str] = None
        self._work_q: "queue.Queue" = queue.Queue()
        self._busy = False

        self._build_ui()
        self._poll_queue()

    # ------------------------------------------------------------------ #
    # UI construction
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        pad = dict(padx=10, pady=6)

        # --- Drop zone / file picker -------------------------------------------------
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)

        self.drop_label = tk.Label(
            top,
            text=("Drag & drop an MP4/MOV here" if _HAS_DND else
                  "Drag & drop needs the optional 'tkinterdnd2' package - use Browse instead"),
            relief="groove", bd=2, height=3, bg="#1e2430", fg="#c9d4e3",
        )
        self.drop_label.pack(side="left", fill="x", expand=True)
        if _HAS_DND:
            self.drop_label.drop_target_register(DND_FILES)
            self.drop_label.dnd_bind("<<Drop>>", self._on_drop)

        ttk.Button(top, text="Browse...", command=self._on_browse).pack(side="left", padx=(10, 0))

        # --- Info panel -----------------------------------------------------------
        info = ttk.LabelFrame(self.root, text="Source file")
        info.pack(fill="x", **pad)
        self.info_var = tk.StringVar(value="No file loaded.")
        ttk.Label(info, textvariable=self.info_var, justify="left").pack(anchor="w", padx=8, pady=6)

        # --- Settings ---------------------------------------------------------------
        settings = ttk.LabelFrame(self.root, text="Settings")
        settings.pack(fill="x", **pad)

        preset_row = ttk.Frame(settings)
        preset_row.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(preset_row, text="Preset:").pack(side="left", padx=(0, 6))
        self.preset_var = tk.StringVar(value="Very Tight")
        self.preset_combo = ttk.Combobox(
            preset_row, textvariable=self.preset_var, state="readonly",
            values=["Natural", "Tight", "Very Tight", "Custom"], width=14,
        )
        self.preset_combo.pack(side="left")
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)

        row1 = ttk.Frame(settings)
        row1.pack(fill="x", padx=8, pady=4)
        self.min_silence = LabeledCombo(row1, "Minimum Silence (frames):", FRAME_PRESET_VALUES, 4)
        self.min_silence.pack(side="left", padx=(0, 24))
        self.threshold = LabeledCombo(row1, "Silence Threshold (dB):", DB_PRESET_VALUES, -40, is_float=True)
        self.threshold.pack(side="left")

        row2 = ttk.Frame(settings)
        row2.pack(fill="x", padx=8, pady=(4, 8))
        self.keep_before = LabeledCombo(row2, "Keep Before Speech (frames):", [0, 1, 2, 3, 4, 5], 1)
        self.keep_before.pack(side="left", padx=(0, 24))
        self.keep_after = LabeledCombo(row2, "Keep After Speech (frames):", [0, 1, 2, 3, 4, 5], 1)
        self.keep_after.pack(side="left")

        for w in (self.min_silence, self.threshold, self.keep_before, self.keep_after):
            w.bind_change(self._on_settings_touched)

        # --- Actions ----------------------------------------------------------------
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", **pad)
        self.analyze_btn = ttk.Button(actions, text="ANALYZE", command=self._on_analyze, state="disabled")
        self.analyze_btn.pack(side="left")
        self.remove_btn = ttk.Button(actions, text="REMOVE SILENCES", command=self._on_remove, state="disabled")
        self.remove_btn.pack(side="left", padx=(10, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(actions, textvariable=self.status_var).pack(side="left", padx=16)
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=160)
        self.progress.pack(side="right")

        # --- Summary ------------------------------------------------------------
        self.summary_var = tk.StringVar(value="")
        ttk.Label(self.root, textvariable=self.summary_var, justify="left").pack(anchor="w", padx=14)

        # --- Waveform canvas ------------------------------------------------------
        wf_frame = ttk.LabelFrame(self.root, text="Timeline (silence regions highlighted)")
        wf_frame.pack(fill="x", padx=10, pady=(6, 6))
        self.wf_canvas = tk.Canvas(wf_frame, height=90, bg="#11151c", highlightthickness=0)
        self.wf_canvas.pack(fill="x", padx=6, pady=6)
        self._wf_photo = None  # keep a reference, tkinter needs it

        # --- Cuts list --------------------------------------------------------------
        list_frame = ttk.LabelFrame(self.root, text="Detected cuts (double-click a row, or use the checkbox column, to toggle it on/off)")
        list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        columns = ("enabled", "index", "start", "end", "duration")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("enabled", text="Cut?")
        self.tree.heading("index", text="#")
        self.tree.heading("start", text="Start")
        self.tree.heading("end", text="End")
        self.tree.heading("duration", text="Duration")
        self.tree.column("enabled", width=60, anchor="center")
        self.tree.column("index", width=50, anchor="center")
        self.tree.column("start", width=140, anchor="center")
        self.tree.column("end", width=140, anchor="center")
        self.tree.column("duration", width=120, anchor="center")
        self.tree.pack(fill="both", expand=True, side="left")
        self.tree.bind("<Double-1>", self._on_toggle_row)
        self.tree.bind("<Button-1>", self._on_tree_click)

        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scroll.set)

    # ------------------------------------------------------------------ #
    # File loading
    # ------------------------------------------------------------------ #
    def _on_browse(self):
        path = filedialog.askopenfilename(
            title="Select a video",
            filetypes=[("Video files", "*.mp4 *.mov *.MP4 *.MOV"), ("All files", "*.*")],
        )
        if path:
            self._load_file(path)

    def _on_drop(self, event):
        path = event.data.strip("{}")
        if path:
            self._load_file(path)

    def _load_file(self, path: str):
        if not os.path.isfile(path):
            messagebox.showerror(APP_TITLE, f"File not found:\n{path}")
            return
        self._set_busy(True, "Reading file info...")

        def work():
            try:
                media = engine.probe_media(path)
                self._work_q.put(("loaded", media))
            except engine.ProbeError as e:
                self._work_q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _on_loaded(self, media: engine.MediaInfo):
        self.media = media
        self.raw_silences = []
        self.analysis = None
        self._clear_tree()
        self.wf_canvas.delete("all")
        self.summary_var.set("")

        audio_info = f"{media.sample_rate} Hz" if media.has_audio else "NO AUDIO TRACK FOUND"
        self.info_var.set(
            f"File: {media.path}\n"
            f"Resolution: {media.width}x{media.height}    "
            f"FPS: {float(media.fps):.3f} ({media.fps.numerator}/{media.fps.denominator})    "
            f"Duration: {_fmt_time(media.duration)}    "
            f"Audio sample rate: {audio_info}"
        )
        self.analyze_btn.config(state=("normal" if media.has_audio else "disabled"))
        self.remove_btn.config(state="disabled")
        if not media.has_audio:
            messagebox.showwarning(APP_TITLE, "This file has no audio track, so silence detection is not possible.")
        self._set_busy(False, "File loaded.")

        # Kick off a waveform render in the background too (nice-to-have,
        # never blocks Analyze).
        threading.Thread(target=self._render_waveform_bg, args=(media.path,), daemon=True).start()

    def _render_waveform_bg(self, path):
        try:
            tmp = os.path.join(tempfile.gettempdir(), "deadair_waveform.png")
            waveform.render_waveform_png(path, tmp, width=1400, height=180)
            self._work_q.put(("waveform", tmp))
        except Exception:
            pass  # waveform preview is optional; never fail the app over it

    # ------------------------------------------------------------------ #
    # Presets
    # ------------------------------------------------------------------ #
    def _on_preset_selected(self, event=None):
        name = self.preset_var.get()
        if name == "Custom":
            return
        s = engine.PRESETS[name]
        self.min_silence.set_value(s.min_silence_frames)
        self.threshold.set_value(s.threshold_db)
        self.keep_before.set_value(s.keep_before_frames)
        self.keep_after.set_value(s.keep_after_frames)

    def _on_settings_touched(self, event=None):
        # Any manual edit to a value switches the preset selector to Custom,
        # but the user can always still pick a preset again afterwards.
        self.preset_var.set("Custom")

    def _current_settings(self) -> engine.CutSettings:
        return engine.CutSettings(
            min_silence_frames=self.min_silence.get_value(),
            threshold_db=self.threshold.get_value(),
            keep_before_frames=self.keep_before.get_value(),
            keep_after_frames=self.keep_after.get_value(),
        )

    # ------------------------------------------------------------------ #
    # Analyze
    # ------------------------------------------------------------------ #
    def _on_analyze(self):
        if self.media is None or self._busy:
            return
        try:
            settings = self._current_settings()
        except ValueError as e:
            messagebox.showerror(APP_TITLE, str(e))
            return

        self._set_busy(True, "Analyzing audio for silence...")

        def work():
            try:
                raw = engine.detect_raw_silences(
                    self.media.path, settings.threshold_db, duration_hint=self.media.duration
                )
                cuts = engine.compute_cuts(raw, self.media, settings)
                result = engine.AnalysisResult(
                    media=self.media, raw_silences=raw, cuts=cuts, settings=settings
                )
                self._work_q.put(("analyzed", result))
            except Exception as e:
                self._work_q.put(("error", f"{e}\n\n{traceback.format_exc()}"))

        threading.Thread(target=work, daemon=True).start()

    def _on_analyzed(self, result: engine.AnalysisResult):
        self.analysis = result
        self._populate_tree(result)
        self._draw_waveform_overlay()
        n = len(result.cuts)
        self.summary_var.set(
            f"Detected {n} silent section(s) qualifying for removal.   "
            f"Estimated time removed: {result.total_removed:.2f}s   "
            f"Estimated output duration: {result.output_duration:.2f}s "
            f"(from {result.media.duration:.2f}s)"
        )
        self.remove_btn.config(state=("normal" if n > 0 else "disabled"))
        self._set_busy(False, "Analysis complete.")

    def _recompute_cuts_only(self):
        """Re-filter existing raw silences with current settings without
        re-running ffmpeg's silencedetect (fast - used only when the dB
        threshold hasn't changed since the last full Analyze)."""
        if self.analysis is None:
            return
        settings = self._current_settings()
        cuts = engine.compute_cuts(self.analysis.raw_silences, self.media, settings)
        self.analysis = engine.AnalysisResult(
            media=self.media, raw_silences=self.analysis.raw_silences, cuts=cuts, settings=settings
        )
        self._populate_tree(self.analysis)
        self._draw_waveform_overlay()

    # ------------------------------------------------------------------ #
    # Cuts list
    # ------------------------------------------------------------------ #
    def _clear_tree(self):
        for row in self.tree.get_children():
            self.tree.delete(row)

    def _populate_tree(self, result: engine.AnalysisResult):
        self._clear_tree()
        for i, c in enumerate(result.cuts, start=1):
            mark = "\u2611" if c.enabled else "\u2610"  # ☑ / ☐
            self.tree.insert(
                "", "end", iid=str(i - 1),
                values=(mark, i, _fmt_time(c.start), _fmt_time(c.end), f"{c.duration:.3f}s"),
            )

    def _on_tree_click(self, event):
        region = self.tree.identify_region(event.x, event.y)
        col = self.tree.identify_column(event.x)
        if region == "cell" and col == "#1":  # the "Cut?" checkbox column
            row = self.tree.identify_row(event.y)
            if row:
                self._toggle_row(row)

    def _on_toggle_row(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self._toggle_row(row)

    def _toggle_row(self, row_iid: str):
        idx = int(row_iid)
        c = self.analysis.cuts[idx]
        c.enabled = not c.enabled
        mark = "\u2611" if c.enabled else "\u2610"
        vals = list(self.tree.item(row_iid, "values"))
        vals[0] = mark
        self.tree.item(row_iid, values=vals)
        self.summary_var.set(
            f"Detected {len(self.analysis.cuts)} silent section(s).   "
            f"Time removed (enabled only): {self.analysis.total_removed:.2f}s   "
            f"Estimated output duration: {self.analysis.output_duration:.2f}s"
        )
        self._draw_waveform_overlay()
        self.remove_btn.config(state=("normal" if any(c.enabled for c in self.analysis.cuts) else "disabled"))

    # ------------------------------------------------------------------ #
    # Waveform drawing
    # ------------------------------------------------------------------ #
    def _draw_waveform_overlay(self):
        self.wf_canvas.delete("all")
        w = max(self.wf_canvas.winfo_width(), 800)
        h = 90

        if self.waveform_png:
            try:
                img = tk.PhotoImage(file=self.waveform_png)
                # tkinter has no built-in resize-to-fit for PhotoImage without
                # PIL; we just draw it at native size anchored top-left, and
                # scale our overlay rectangles to whatever width it has.
                self._wf_photo = img
                self.wf_canvas.create_image(0, 0, anchor="nw", image=img)
                w = img.width()
                h = img.height()
                self.wf_canvas.configure(height=h)
            except Exception:
                pass

        if self.analysis is None or self.media is None or self.media.duration <= 0:
            return

        dur = self.media.duration
        for c in self.analysis.cuts:
            x1 = (c.start / dur) * w
            x2 = (c.end / dur) * w
            color = "#e05252" if c.enabled else "#555f6e"
            self.wf_canvas.create_rectangle(x1, 0, x2, h, fill=color, stipple="gray50", outline="")

    # ------------------------------------------------------------------ #
    # Remove silences
    # ------------------------------------------------------------------ #
    def _on_remove(self):
        if self.analysis is None or self._busy:
            return
        default_name = os.path.splitext(os.path.basename(self.media.path))[0] + "_NO_SILENCE.mp4"
        out_path = filedialog.asksaveasfilename(
            title="Save cleaned video as...",
            initialfile=default_name,
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")],
        )
        if not out_path:
            return
        if os.path.abspath(out_path) == os.path.abspath(self.media.path):
            messagebox.showerror(APP_TITLE, "Output file must be different from the source file.")
            return

        self._set_busy(True, "Removing silences (this can take a while)...")

        def work():
            try:
                keep = engine.keep_segments_from_cuts(self.analysis.cuts, self.media)
                if not keep:
                    self._work_q.put(("error", "Every frame would be cut - nothing left to output. "
                                                 "Disable some cuts or adjust your settings."))
                    return
                plan = cutter.build_cut_plan(self.media, keep, out_path)
                cutter.run_cut(plan)
                self._work_q.put(("removed", out_path))
            except Exception as e:
                self._work_q.put(("error", f"{e}\n\n{traceback.format_exc()}"))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Busy state / background-thread <-> UI-thread plumbing
    # ------------------------------------------------------------------ #
    def _set_busy(self, busy: bool, status: str = ""):
        self._busy = busy
        self.status_var.set(status)
        if busy:
            self.progress.start(12)
            self.analyze_btn.config(state="disabled")
            self.remove_btn.config(state="disabled")
        else:
            self.progress.stop()
            if self.media is not None and self.media.has_audio:
                self.analyze_btn.config(state="normal")
            if self.analysis is not None and any(c.enabled for c in self.analysis.cuts):
                self.remove_btn.config(state="normal")

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._work_q.get_nowait()
                if kind == "loaded":
                    self._on_loaded(payload)
                elif kind == "analyzed":
                    self._on_analyzed(payload)
                elif kind == "waveform":
                    self.waveform_png = payload
                    self._draw_waveform_overlay()
                elif kind == "removed":
                    self._set_busy(False, "Done.")
                    messagebox.showinfo(APP_TITLE, f"Cleaned video saved to:\n{payload}")
                elif kind == "error":
                    self._set_busy(False, "Error.")
                    messagebox.showerror(APP_TITLE, str(payload))
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)


def main():
    root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
    try:
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()

"""
selftest.py
A headless, no-GUI, end-to-end proof that the packaged app actually works
on the machine it's running on: it uses THIS build's own resolved
ffmpeg/ffprobe (bundled inside the .app when frozen) to generate a tiny
synthetic clip with known speech/silence timing, run it through the full
probe -> detect -> compute_cuts -> cut pipeline, and assert the output is
exactly what it should be.

Run via:  MyApp.app/Contents/MacOS/MyApp --selftest
(or `python3 -m app.selftest` from source)

Exit code 0 = pass, 1 = fail. Prints a human-readable report either way.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

from . import engine
from . import cutter


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{proc.stderr[-2000:]}")


def _build_test_clip(out_path: str, fps: str = "30") -> None:
    """1.0s speech / 0.333s silence(10f) / 1.5s speech / 0.067s silence(2f,
    below default min) / 2.0s speech / 0.167s silence(5f) / 1.0s speech,
    encoded in a single pass so there are no AAC-boundary artifacts."""
    cmd = [
        engine.ffmpeg_bin(), "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration=1.0",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.0",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration=0.333333333",
        "-f", "lavfi", "-i", "anullsrc=sample_rate=48000:channel_layout=stereo:duration=0.333333333",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration=1.5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.5",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration=0.066666667",
        "-f", "lavfi", "-i", "anullsrc=sample_rate=48000:channel_layout=stereo:duration=0.066666667",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration=2.0",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2.0",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration=0.166666667",
        "-f", "lavfi", "-i", "anullsrc=sample_rate=48000:channel_layout=stereo:duration=0.166666667",
        "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate={fps}:duration=1.0",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1.0",
        "-filter_complex",
        "[0:v][1:a][2:v][3:a][4:v][5:a][6:v][7:a][8:v][9:a][10:v][11:a][12:v][13:a]"
        "concat=n=7:v=1:a=1[outv][outa]",
        "-map", "[outv]", "-map", "[outa]",
        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "16", "-c:a", "aac", "-b:a", "192k",
        out_path,
    ]
    _run(cmd)


def run() -> int:
    print(f"[selftest] ffmpeg  = {engine.ffmpeg_bin()}")
    print(f"[selftest] ffprobe = {engine.ffprobe_bin()}")

    with tempfile.TemporaryDirectory(prefix="deadair_selftest_") as tmp:
        src = os.path.join(tmp, "synthetic_input.mp4")
        out = os.path.join(tmp, "synthetic_output.mp4")

        print("[selftest] generating synthetic test clip...")
        _build_test_clip(src)

        print("[selftest] probing media...")
        media = engine.probe_media(src)
        assert media.width == 320 and media.height == 240, f"unexpected resolution: {media.width}x{media.height}"
        assert abs(float(media.fps) - 30.0) < 0.01, f"unexpected fps: {media.fps}"
        assert media.has_audio, "expected an audio stream"
        print(f"[selftest]   -> {media.width}x{media.height} @ {float(media.fps):.3f}fps, "
              f"{media.duration:.3f}s, audio {media.sample_rate}Hz")

        print("[selftest] detecting silence...")
        raw = engine.detect_raw_silences(src, threshold_db=-40.0, duration_hint=media.duration)
        assert len(raw) == 3, f"expected 3 raw silences, got {len(raw)}: {raw}"
        print(f"[selftest]   -> {len(raw)} raw silence regions found (expected 3)")

        print("[selftest] computing cuts (min=4 frames, keep=1/1)...")
        settings = engine.CutSettings(min_silence_frames=4, threshold_db=-40.0,
                                       keep_before_frames=1, keep_after_frames=1)
        cuts = engine.compute_cuts(raw, media, settings)
        assert len(cuts) == 2, f"expected 2 qualifying cuts (the 2-frame silence should be skipped), got {len(cuts)}"
        print(f"[selftest]   -> {len(cuts)} cuts qualify (expected 2; the sub-threshold 2-frame gap correctly skipped)")

        print("[selftest] cutting video...")
        keep = engine.keep_segments_from_cuts(cuts, media)
        plan = cutter.build_cut_plan(media, keep, out)
        assert plan.kept_frame_count == 171, f"expected 171 kept frames, plan says {plan.kept_frame_count}"
        cutter.run_cut(plan)

        print("[selftest] verifying output...")
        out_media = engine.probe_media(out)
        # Confirm actual frame count in the encoded file matches the plan.
        probe_cmd = [engine.ffprobe_bin(), "-v", "error", "-select_streams", "v",
                     "-count_frames", "-show_entries", "stream=nb_read_frames",
                     "-of", "default=noprint_wrappers=1:nokey=1", out]
        proc = subprocess.run(probe_cmd, capture_output=True, text=True)
        actual_frames = int(proc.stdout.strip())
        assert actual_frames == 171, f"expected 171 frames in output file, ffprobe counted {actual_frames}"
        print(f"[selftest]   -> output has {actual_frames} frames, {out_media.duration:.3f}s "
              f"(gap-free, in sync)")

    print("[selftest] ALL CHECKS PASSED")
    return 0


def main():
    try:
        return run()
    except Exception as e:
        print(f"[selftest] FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""
waveform.py
Renders a waveform PNG for the timeline preview using ffmpeg's built-in
`showwavespic` filter. Deliberately avoids numpy/matplotlib so the packaged
.exe stays small and simple - ffmpeg is already a hard requirement of this
whole app.
"""

from __future__ import annotations

import subprocess

from .engine import ffmpeg_bin


def render_waveform_png(path: str, out_png: str, width: int, height: int) -> None:
    cmd = [
        ffmpeg_bin(), "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", path,
        "-filter_complex",
        f"[0:a]aformat=channel_layouts=mono,"
        f"showwavespic=s={width}x{height}:colors=0x4C9AFF",
        "-frames:v", "1",
        out_png,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Waveform render failed:\n{proc.stderr[-2000:]}")
