"""Build reproducible, non-overlapping MMLU test subsets from the public CSV archive.

Download https://opencompass.oss-cn-shanghai.aliyuncs.com/datasets/data/mmlu.zip
and pass its path as the first argument. The official task is multiple-choice QA;
the explanation and calculator variants below add platform-specific obligations.
"""
import csv
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

archive = Path(sys.argv[1])
root = Path(__file__).resolve().parents[1]
source = "https://github.com/hendrycks/test"
archive_url = "https://opencompass.oss-cn-shanghai.aliyuncs.com/datasets/data/mmlu.zip"
archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()

with zipfile.ZipFile(archive) as bundle:
    def rows(subject):
        name = f"mmlu/test/{subject}_test.csv"
        return list(csv.reader(io.TextIOWrapper(bundle.open(name), encoding="utf-8")))

    def make(subject, indexes, style):
        records = rows(subject)
        cases = []
        for index in indexes:
            question, *rest = records[index]
            choices, answer = rest[:4], rest[4].strip().upper()
            if answer not in "ABCD" or len(choices) != 4:
                raise ValueError((subject, index))
            stem = f"MMLU {subject.replace('_', ' ')} · test #{index + 1}\n{question.strip()}\n" + "\n".join(f"{letter}. {choice.strip()}" for letter, choice in zip("ABCD", choices))
            if style == "answer":
                instruction = "Select the best option. End with exactly `Answer: A`, `Answer: B`, `Answer: C`, or `Answer: D`."
            elif style == "reason":
                instruction = "Solve the question with at least two numbered reasoning steps. End with exactly `Answer: X`, replacing X with A, B, C, or D."
            elif style == "explain":
                instruction = "Explain the choice in at least two numbered reasons, then end with exactly `Answer: X`, replacing X with A, B, C, or D."
            elif style == "tool":
                instruction = "Use the calculator tool to check a numerical computation before answering. Give at least two numbered steps and end with exactly `Answer: X`, replacing X with A, B, C, or D."
            else:
                raise ValueError(style)
            cases.append({"input_text": f"{stem}\n{instruction}", "expected": answer, "expected_tool": "calculator" if style == "tool" else "", "source_ref": f"{subject}_test.csv:{index + 1}"})
        return cases

    specs = [
        ("mmlu-general-60", "MMLU · 多学科知识与推理 60", "general", "answer", [("machine_learning", range(0, 15)), ("formal_logic", range(0, 15)), ("professional_law", range(0, 15)), ("high_school_world_history", range(0, 15))]),
        ("mmlu-math-reasoning-60", "MMLU · 复杂计算与分步推理 60", "complex_calculation", "reason", [("college_mathematics", range(0, 20)), ("high_school_statistics", range(0, 20)), ("professional_accounting", range(0, 20))]),
        ("mmlu-explanation-40", "MMLU · 解释生成与任务拆解 40", "generation_and_decomposition", "explain", [("professional_law", range(15, 35)), ("high_school_world_history", range(15, 35))]),
        ("mmlu-tool-reasoning-40", "MMLU · 数值推理工具调用 40", "complex_tool_calling", "tool", [("high_school_statistics", range(20, 40)), ("professional_accounting", range(20, 40))]),
    ]
    for filename, title, category, style, groups in specs:
        cases = [case for subject, indexes in groups for case in make(subject, indexes, style)]
        metrics = ["model_online", "final_answer", "completion", "cost"]
        if style != "answer":
            metrics.append("steps_present")
        if style == "tool":
            metrics.append("tool")
        payload = {
            "name": title,
            "source": source,
            "archive_url": archive_url,
            "archive_sha256": archive_hash,
            "license": "MIT",
            "selection": "MMLU original test rows; fixed subject and 0-based index ranges in scripts/build_mmlu_eval.py. Explanation and tool obligations are added by this platform and are not native MMLU scores.",
            "category": category,
            "metrics": metrics,
            "cases": cases,
        }
        destination = root / "backend/fixtures" / f"{filename}.json"
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        print(destination.name, len(cases))
