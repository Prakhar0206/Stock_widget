"""Generates tests/data/sample_day.json: a deterministic synthetic regular session (390 one-minute
readings) for AAPL (SELL) and TTWO (BUY), with a gap, a flat stretch, a zero tick and a bad tick."""
import json, random
from pathlib import Path

rng = random.Random(20261014)
def walk(start, n, vol):
    p, out = start, []
    for _ in range(n):
        p = max(1.0, p * (1 + rng.gauss(0, vol)))
        out.append(round(p, 2))
    return out

aapl = walk(201.00, 390, 0.0009)
aapl[100:130] = [aapl[99]] * 30                                  # flat stretch
for i in range(200, 390):                                       # gap up at minute 200
    aapl[i] = round(aapl[i] * 1.045, 2)
aapl[250] = 0                                                    # zero tick
aapl[300] = round(aapl[299] * 1.6, 2)                            # bad tick (>25%)
ttwo = walk(206.00, 390, 0.0012)
for i in range(150, 390):
    ttwo[i] = round(ttwo[i] * 0.965, 2)                          # gap down at minute 150
Path(__file__).resolve().parent.parent.joinpath("tests/data/sample_day.json").write_text(
    json.dumps({"AAPL": aapl, "TTWO": ttwo, "prev_close": {"AAPL": 200.0, "TTWO": 205.0}}))
