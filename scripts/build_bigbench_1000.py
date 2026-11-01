"""Reproduce a 1,000-case subset of BIG-bench's simple_arithmetic task.

Source: https://github.com/google/BIG-bench/blob/main/bigbench/benchmark_tasks/simple_arithmetic/task.py
License: Apache-2.0. This subset uses the task's n-digit addition rule, with a
fixed Python RNG seed and unique operands; it is generated, not a copy of the
repository's 30 static JSON examples. Benchmark data is for evaluation only.
"""
import json
import random
from pathlib import Path

rng = random.Random(42)
cases = []
for digits in (2, 3, 4, 5):
    seen = set()
    while len(seen) < 250:
        a = rng.randrange(10 ** digits)
        b = rng.randrange(10 ** digits)
        if (a, b) in seen:
            continue
        seen.add((a, b))
        cases.append({"input_text": f"{a} + {b} =", "expected": str(a + b)})
payload = {
    "name": "BIG-bench simple_arithmetic · 1000 unique additions",
    "source": "https://github.com/google/BIG-bench/blob/main/bigbench/benchmark_tasks/simple_arithmetic/task.py",
    "license": "Apache-2.0",
    "selection": "Task-rule-generated, Python random seed 42; 250 unique operand pairs each for 2, 3, 4 and 5 digits. Evaluation use only.",
    "metrics": ["exact", "completion", "latency", "cost"],
    "cases": cases,
}
path = Path(__file__).resolve().parents[1] / "backend/fixtures/bigbench-simple-arithmetic-1000.json"
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
print(path, len(cases))
