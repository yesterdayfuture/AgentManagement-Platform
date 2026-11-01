# MMLU 综合能力评测

## 在系统中查看

本轮使用现有「客服中心」空间，没有新建空间。平台管理员登录后默认查看全部空间；客服中心成员默认查看所属空间。在「评测中心」搜索 `MMLU`，可以查看四组 MMLU 数据集和逐条用例；另有一组 BIG-bench 文本编辑数据集。评测智能体是 **DeepSeek 综合能力评测智能体**（本地 ID 31，模型代码 `deepseek-flash`）。进入该智能体工作室，点击版本行的「查看」，可打开该版本关联的评测报告。每个数据集操作列的「查看历史评测」支持检索、分页、调整页大小、按结果筛选和逐题查看。新增通过发起新评测完成；历史输出与评分作为快照保留，编辑只修改备注。

运行前需明确选择智能体版本；报告固定记录所选版本编号。平台管理员可以选择全部智能体，包括未分配空间的智能体；跨空间评测在目标智能体所属空间执行，未分配空间的智能体在所选数据集的空间执行。这两类报告仅平台管理员可见。其他成员只可评测数据集所属空间内的对象。工作室中未通过评测的版本提供「评测此版本」入口；进入评测中心后选择数据集并点击「运行」，智能体和版本会预选。未分配空间的智能体须待该版本评测通过后才能发布、设为线上或灰度。

运行评测时输入最低通过率（百分比）。单条用例必须满足所选的全部指标；通过用例数除以总用例数达到门槛，整份报告才通过。工作流可另设发布门槛，发布时检查对应版本、数据集内容及智能体版本仍与报告相符。

## 来源与选择

MMLU 来源为 [Hendrycks 等人的项目仓库](https://github.com/hendrycks/test)，使用由 [OpenCompass 提供的 MMLU 镜像归档](https://opencompass.oss-cn-shanghai.aliyuncs.com/datasets/data/mmlu.zip)中的 `test` 划分。数据集采用 MIT 许可。脚本固定选择科目和原始行号，总共 200 条，四组互不重叠：

| 数据集 | 科目与抽样 | 条数 | 额外检查 |
| --- | --- | ---: | --- |
| 多学科知识与推理 | machine_learning、formal_logic、professional_law、high_school_world_history 各前 15 条 | 60 | 最终选项、在线模型、完成、成本 |
| 复杂计算与分步推理 | college_mathematics、high_school_statistics、professional_accounting 各前 20 条 | 60 | 最终选项、编号步骤、在线模型、完成、成本 |
| 解释生成与任务拆解 | professional_law、high_school_world_history 各第 16–35 条 | 40 | 最终选项、编号理由、在线模型、完成、成本 |
| 数值推理工具调用 | high_school_statistics、professional_accounting 各第 21–40 条 | 40 | 最终选项、编号步骤、`calculator` 轨迹、在线模型、完成、成本 |

另取 [BIG-bench simple_text_editing](https://github.com/google/BIG-bench/blob/main/bigbench/benchmark_tasks/simple_text_editing/task.json) 47 条原始样例，以精确匹配评估受约束文本生成。该组的许可为 Apache-2.0。

MMLU 原始数据只提供四选一正确答案。编号理由、步骤形式和计算器调用是本项目加在原题上的任务要求，**不是 MMLU 官方能力标签或标准分数**。编号检查只验证结构，不代表推理过程语义正确；工具组同时验证实际 `calculator` 执行轨迹。开放式长文生成质量仍需人工或更合适的专项数据集评估。

## 本轮实测结果

| 能力 | 通过/总数 | 通过率 | 在线模型 | 费用（元） | 报告 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 多学科知识与推理 | 59/60 | 98.3% | 60/60 | 0.174446 | #10 |
| 复杂计算与分步推理 | 55/60 | 91.7% | 60/60 | 0.154741 | #11 |
| 解释生成与任务拆解 | 37/40 | 92.5% | 40/40 | 0.328777 | #12 |
| 数值推理工具调用 | 33/40 | 82.5% | 40/40 | 0.118962 | #15 |
| 受约束文本生成 | 38/47 | 80.9% | 47/47 | 0.146983 | #16 |
| **合计** | **222/247** | **89.9%** | **247/247** | **0.923909** | **5 份最终报告** |

五份最终报告均达到本轮设置的 80% 门槛。工具组 40/40 实际调用了 `calculator`；其中 36 条最终选项正确、35 条满足编号步骤要求，33 条满足全部所选指标。第一次运行到工具组第 4 条时，现有客服中心的 100 万 token 额度用尽，工具组和文本生成组因此留下失败报告 #13、#14。额度通过现有管理接口提高到 150 万 token 后仅重跑这两组，并保留首次失败记录以便追溯。上述表格只统计重跑后的完整在线结果。费用按系统模型计费配置记录，并非账单实付。

## 复现

```bash
PYTHONPATH=backend backend/.venv/bin/python scripts/build_mmlu_eval.py /path/to/mmlu.zip
PYTHONPATH=backend backend/.venv/bin/python scripts/setup_mmlu_live_eval.py
PYTHONPATH=backend backend/.venv/bin/python scripts/run_mmlu_live_eval.py
```

构建脚本使用本地 MMLU 归档，固定抽样结果保存在 `backend/fixtures/mmlu-*.json`，包含原始行号、来源 URL、SHA-256、原答案和评测输入。安装脚本在现有空间中创建或补齐对象；运行脚本调用已配置的 DeepSeek 端点，以 80% 为报告通过门槛，将结果写入系统 `eval_runs` 和 `backend/reports/mmlu-live-*.json`。逐条结果保留模型在线标记、实际工具轨迹、耗时、成本及各指标分数。密钥从系统密钥库读取，不包含在报告中。
