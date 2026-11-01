r"""Build a self contained ghost car replay of two qualifying laps.

Run from the repository folder:

    python make_animation.py
    python make_animation.py --output replay.html

The script downloads both laps with tif1, puts them on a common clock, works out
the gap, mini sector winners, braking points and data dropouts, and writes one HTML
file from animation_template.html. Open that file in a browser to watch it.
"""

from __future__ import annotations

import argparse
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import tif1

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "animation_template.html"
DEFAULT_OUTPUT = HERE / "monza_pole_animation.html"

YEAR, EVENT, SESSION = 2025, "Italian Grand Prix", "Qualifying"
TRACK_NAME = "Monza"
DRIVERS = (("VER", "#3b82f6"), ("NOR", "#ff8000"))

FRAME_STEP_S = 0.05          # one animation frame per 0.05 s of lap time
CLOCK_RUNOUT_S = 1.5         # keeps the clock running briefly after the flag
GAP_CURVE_POINTS = 1500
MINI_SECTORS = 24
BRAKE_THRESHOLD = 0.5
BRAKE_SEARCH_FROM_M = 600    # skips the start line, so this finds Turn 1
BRAKE_RESOLUTION_M = 10      # public telemetry cannot honestly resolve less
EVENT_LEAD_IN_M = 120
EVENT_RUN_OUT_M = 220
DROPOUT_MIN_S = 0.5
CHANNELS = ("d", "x", "y", "v", "thr", "brk", "g")


@dataclass(frozen=True)
class Lap:
    """One driver's fastest lap, as time ordered telemetry arrays."""

    code: str
    color: str
    laptime: float
    t: np.ndarray
    d: np.ndarray
    x: np.ndarray
    y: np.ndarray
    v: np.ndarray
    thr: np.ndarray
    brk: np.ndarray
    g: np.ndarray

    def resample(self, clock: np.ndarray) -> dict[str, np.ndarray]:
        """Interpolate every channel onto a shared clock."""
        return {name: np.interp(clock, self.t, getattr(self, name)) for name in CHANNELS}

    def time_at(self, distance: np.ndarray) -> np.ndarray:
        """Lap time at which this car reached each distance."""
        return np.interp(distance, self.d, self.t)


def load_lap(session, code: str, color: str) -> Lap:
    """Load a driver's fastest lap, dropping repeated timestamps."""
    lap = session.laps.pick_drivers(code).pick_fastest()
    tel = lap.get_car_data().add_distance().sort_values("Time")
    t = tel["Time"].dt.total_seconds().to_numpy()
    keep = np.concatenate([[True], np.diff(t) > 0])
    tel, t = tel[keep], t[keep]
    result = Lap(
        code=code,
        color=color,
        laptime=pd.Timedelta(lap["LapTime"]).total_seconds(),
        t=t,
        d=tel["Distance"].to_numpy(dtype=float),
        x=tel["X"].to_numpy(dtype=float),
        y=tel["Y"].to_numpy(dtype=float),
        v=tel["Speed"].to_numpy(dtype=float),
        thr=tel["Throttle"].to_numpy(dtype=float),
        brk=tel["Brake"].astype(float).to_numpy(),
        g=tel["nGear"].to_numpy(dtype=float),
    )
    print(f"{code}: {len(t)} samples, lap {result.laptime:.3f} s")
    return result


def first_brake_distance(lap: Lap) -> float | None:
    """Distance of the first braking point after the search start."""
    braking = (lap.brk > BRAKE_THRESHOLD) & (lap.d > BRAKE_SEARCH_FROM_M)
    return float(lap.d[braking][0]) if braking.any() else None


def key_events(a: Lap, b: Lap) -> list[dict]:
    """Describe the braking comparison into Turn 1."""
    brake_a, brake_b = first_brake_distance(a), first_brake_distance(b)
    if not (brake_a and brake_b):
        return []
    later = a.code if brake_a > brake_b else b.code
    metres = max(BRAKE_RESOLUTION_M, int(round(abs(brake_a - brake_b), -1)))
    return [{
        "from": min(brake_a, brake_b) - EVENT_LEAD_IN_M,
        "to": max(brake_a, brake_b) + EVENT_RUN_OUT_M,
        "marks": [{"code": a.code, "d": brake_a}, {"code": b.code, "d": brake_b}],
        "text": f"Turn 1: {later} brakes about {metres} m later",
    }]


def data_dropouts(laps: tuple[Lap, Lap]) -> list[dict]:
    """Find each lap's largest hole between samples, if it is long enough to matter."""
    holes = []
    for lap in laps:
        i = int(np.argmax(np.diff(lap.t)))
        duration = lap.t[i + 1] - lap.t[i]
        if duration > DROPOUT_MIN_S:
            holes.append({
                "code": lap.code,
                "from": float(lap.d[i]),
                "to": float(lap.d[i + 1]),
                "secs": round(float(duration), 1),
            })
    return holes


def track_corners(session) -> list[dict]:
    """Corner labels for the map. They are decoration, so the replay survives without them."""
    try:
        corners = session.get_circuit_info().corners
    except Exception as error:  # the provider raises several types when this is missing
        print(f"No corner labels: {error}")
        return []
    return [{
        "n": str(int(c["Number"])) + str(c.get("Letter", "") or ""),
        "x": float(c["X"]),
        "y": float(c["Y"]),
    } for _, c in corners.iterrows()]


def rounded(values, places: int = 1) -> list[float]:
    """Round for a smaller HTML file."""
    return [round(float(v), places) for v in values]


def driver_payload(lap: Lap, frames: dict[str, np.ndarray]) -> dict:
    return {
        "code": lap.code,
        "color": lap.color,
        "laptime": lap.laptime,
        "x": rounded(frames["x"], 0),
        "y": rounded(frames["y"], 0),
        "d": rounded(frames["d"], 1),
        "v": rounded(frames["v"], 0),
        "thr": rounded(frames["thr"], 0),
        "brk": rounded(frames["brk"], 2),
        "g": rounded(frames["g"], 0),
    }


def build_payload(session, a: Lap, b: Lap) -> dict:
    """Everything the replay page needs, on one clock."""
    clock = np.arange(0, max(a.laptime, b.laptime) + CLOCK_RUNOUT_S, FRAME_STEP_S)
    frames_a, frames_b = a.resample(clock), b.resample(clock)

    # The gap at each frame is measured where the trailing car is right now:
    # how much later did B reach that spot than A?
    trailing = np.minimum(frames_a["d"], frames_b["d"])
    gap_frames = b.time_at(trailing) - a.time_at(trailing)

    lap_length = min(a.d.max(), b.d.max())
    distance_grid = np.linspace(0, lap_length, GAP_CURVE_POINTS)
    gap_curve = b.time_at(distance_grid) - a.time_at(distance_grid)

    edges = np.linspace(0, lap_length, MINI_SECTORS + 1)
    winners = np.where(np.diff(a.time_at(edges)) <= np.diff(b.time_at(edges)), 0, 1)

    path_length = np.sum(np.hypot(np.diff(a.x), np.diff(a.y)))

    return {
        "title": f"{TRACK_NAME} {YEAR} {SESSION}",
        "step": FRAME_STEP_S,
        "n": len(clock),
        "lapLen": float(lap_length),
        "mPerUnit": float(a.d.max() / path_length),
        "officialGap": round(b.laptime - a.laptime, 3),
        "gap": rounded(gap_frames, 3),
        "drivers": [driver_payload(a, frames_a), driver_payload(b, frames_b)],
        "track": {"x": rounded(a.x, 0), "y": rounded(a.y, 0), "d": rounded(a.d, 1)},
        "gapCurve": {"d": rounded(distance_grid, 1), "g": rounded(gap_curve, 3)},
        "dom": {"edges": rounded(edges, 1), "winner": winners.tolist()},
        "events": key_events(a, b),
        "holes": data_dropouts((a, b)),
        "corners": track_corners(session),
    }


def write_page(payload: dict, output: Path) -> None:
    """Fill the template with the data, giving one file that works offline."""
    template = TEMPLATE.read_text(encoding="utf-8")
    output.write_text(template.replace("__DATA__", json.dumps(payload, separators=(",", ":"))), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the ghost car replay of two qualifying laps.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="where to write the page")
    parser.add_argument("--copy-to", type=Path, help="also copy the finished page here, for example into the portfolio")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session = tif1.get_session(YEAR, EVENT, SESSION)
    a, b = (load_lap(session, code, color) for code, color in DRIVERS)
    write_page(build_payload(session, a, b), args.output)
    print(f"Done. Open {args.output} in your browser.")
    if args.copy_to:
        shutil.copyfile(args.output, args.copy_to)
        print(f"Copied to {args.copy_to}")


if __name__ == "__main__":
    main()
