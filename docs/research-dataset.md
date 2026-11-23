# 研究数据集

本页是「结构化病历 → 研究数据集」这层的 SSOT。上游（采集包怎样清洗成结构化
病历）见[病历收集与结构化](clinical-record.md)。研究数据集服务于跨病例的
统一管理与统计：把已清洗的字段抽成**固定列**，补上与原文并列的**归一化编码**，
并输出可直接纵向合并的长表。

## 为什么分两层

结构化病历的职责是忠实：保留文书原文、逐节标注来源、把缺口与冲突列出来。
研究分析需要的是另一件事——同一列在不同病例里含义一致，可以直接拼接和统计。
两层分开，两边的约束才不会互相污染：归一化只增加列，不改写原文；原文里的
措辞差异仍然留在原处供人工核对。

```text
采集包（仓库外）→ 结构化病历（原文 + provenance + 缺口/冲突）
                        ↓  records/research_dataset.py
                  研究数据集（固定列 + 归一化码，长表，可跨病例合并）
                        ↓
                  统计分析 / 队列管理；归一化存疑处回结构化病历核对原文
```

## 表与列

列顺序是合同的一部分：两张表只要表头一致就能纵向拼接，因此新增病例不会改变
已有列的含义。列定义以 `src/med_autopractice/records/research_dataset.py`
的 `TABLES` 为准（有测试覆盖），逐列说明如下。

| 表 | 一行代表 | 主要列 |
|---|---|---|
| `subject` | 一位患者 | 研究编号、站点、性别码、出生日期、末次就诊年龄、民族、就诊次数 |
| `encounter` | 一次住院 | 就诊次序、就诊标识、科室、入出院时间、住院天数（记录值 / 按日期计算）、床位、来源文书 |
| `condition` | 一条诊断记录 | 就诊次序、阶段（入院/出院）、阶段内序号、诊断原文、诊断归一键、首位出院诊断、住院期间新增 |
| `medication` | 一条出院带药 | 药品、单次量+单位、途径原文+途径码、频次原文+频次码、次/日、总量+单位、解析状态、原文行 |
| `measurement` | 一条阳性检查/检验 | 检查日期、项目名、类别码、结果原文、原文行 |
| `procedure` | 一台手术或操作 | 日期、名称、麻醉方式、提取方式、来源段落 |
| `qc_issue` | 一条数据质量问题 | 类型（冲突/缺口/采集范围/院外既往）、字段、详情、处置 |
| `problem_list` | 一个归并后的问题 | 归一键、原文、涉及就诊数、首末就诊、最近一次是否仍记载、住院期间新增的就诊 |
| `quality_summary` | 一位患者一行 | 各表条数与需人工确认数，用于快速筛出需要复核的病例 |

写分析代码时用的确切列名如下（顺序即 CSV 表头顺序；新增列必须同时更新本表，
有测试防止漂移）：

```text
subject          subject_id, record_id, site_id, gender_code, birth_date,
                 age_at_last_visit, ethnicity, nationality, marital_status,
                 encounter_count
encounter        subject_id, visit_seq, encounter_id, department,
                 admission_datetime, discharge_datetime, los_days_recorded,
                 los_days_computed, bed_no, source_document
condition        subject_id, visit_seq, phase, seq_in_phase, term_raw,
                 term_norm, first_discharge_diagnosis, added_during_stay,
                 distinct_visits
medication       subject_id, visit_seq, seq_in_visit, drug_name, dose_value,
                 dose_unit, route_raw, route_code, frequency_raw,
                 frequency_code, doses_per_day, total_quantity, total_unit,
                 parse_status, raw
measurement      subject_id, visit_seq, seq_in_visit, performed_date,
                 test_name, modality_code, result_text, raw
procedure        subject_id, visit_seq, procedure_date, procedure_name,
                 anesthesia, extraction, source_text
qc_issue         subject_id, visit_seq, kind, field, detail, resolution
problem_list     subject_id, term_norm, term_raw, visit_count,
                 first_visit_seq, last_visit_seq, in_latest_visit,
                 added_during_stay_visits
quality_summary  subject_id, visits, conditions, medications, measurements,
                 procedures, qc_issues, conditions_added_during_stay,
                 unparsed_medications, problems_needing_confirmation
```

## 三条数据规则

1. **归一化与原文并列。** 每个归一化字段旁边都保留它来自的原文（`term_raw`
   与 `term_norm`、`route_code` 与 `route_raw`、`frequency_code` 与
   `frequency_raw`、`result_text` 与 `raw`）。归一化只增加列，不替换、不覆盖。
2. **缺失一律留空。** 文书没写的字段留空，不用医学常识、上一次就诊或“常见
   默认值”补齐。例如带药原文只写「示例降糖药按既往用药继续服用」时，剂量、
   途径、频次全部为空，`parse_status` 记为 `unparsed`，供人工回原文确认。
3. **不做临床判断。** 诊断只按文字归并；措辞相近但不同的写成
   `problem_list` 的多行，交由人工判断是否同一问题。代码不合并、不推断、不排序
   临床优先级。

## 归一化规则

归一化只处理有确定映射的取值；无法确定时写 `OTHER` 或留空，并靠原文列回溯。

| 字段 | 规则 | 示例（合成） |
|---|---|---|
| 性别 | 男/男性 → `M`，女/女性 → `F`，其余为空 | `男` → `M` |
| 途径 | 中文途径名映射为缩写码 | `口服` → `PO`；`静脉滴注`/`静滴` → `IV_DRIP` |
| 频次 | 映射为码，并折算 `doses_per_day` | `每日三次` → `TID` / `3.0`；`每8小时一次` → `Q8H` / `3.0`；`每晚一次` → `QN` / `1.0`；按需类 → `PRN` / 空 |
| 剂量 | 拆成数值与单位，字母单位统一大小写 | `0.50 mg` → `0.5` / `mg`；`0.20 g` → `0.2` / `g` |
| 检查类别 | 仅当项目名含明确关键词时归类 | 含 `MR`/`磁共振` → `MR`；含 `CT` → `CT`；含 `心电图` → `ECG`；都不含 → `OTHER` |
| 住院天数 | 记录值原样保留，另按入出院日期算一列 | 记录 8 天、按日期算 7 天时两列都留 |
| 年龄 | 由出生日期与就诊日期计算整岁 | 出生 1960-05-06、就诊 2025-11-03 → 65 |

频次与剂量不吻合、记录与计算不一致这类差异**不报错、不修正**，两列并存，让
复核者看到差异本身。这正是研究数据里最有价值的一类信号。

## 去标识

`subject_id` 由站点与住院号派生的散列前 12 位加上前缀 `S` 构成（例如
`S1a2b3c4d5e6f`），同一站点同一住院号重复导出得到相同编号，便于跨批次合并；
表内不出现姓名、身份证号、电话、地址等直接标识（有测试覆盖）。

这个编号是**研究内配对的键，不是匿名化承诺**：真实患者数据仍须按院方批准的
受控存储与访问控制保存，导出范围与保留期限由部署政策决定，不要把它当作可以
随意外发的脱敏数据。

## 命令与产物

```sh
PYTHONPATH=src python3 scripts/structure_record.py \
  --collection /absolute/path/outside/repo/history-<id>.json \
  --out-dir    /absolute/path/outside/repo/records \
  --record-id  history-<id> \
  --review-notes /absolute/path/outside/repo/review-notes.json
```

```text
<out-dir>/<record-id>.structured.json   机器可读的结构化病历
<out-dir>/<record-id>.structured.md     结构化病历的审阅稿
<out-dir>/<record-id>.summary.md        研究阅读版（结构化病历 → 问题清单 → 病历原文）
<out-dir>/tables/<table>.csv            研究数据集长表（9 张，权限 600）
```

`--formats` 可选 `json,md,summary,csv`（默认全部）；`--tables-dir` 可把长表
写到别处；命令只打印计数，不回显患者内容，输出可以留在任务日志里。

## 复核流程（研究阅读版）

研究阅读版的章节顺序是固定的，按“先看数据、再看问题、最后回原文”排列：

1. **结构化病历（研究数据集）**。阅读视图展示病例重点、原文及相应的归一化结果。完整九表见 CSV 产物。
2. **问题清单与需核实项**——诊断归并结果、文书层面的差异与缺口，以及每条
   需要人工确认的具体问题；
3. **病历原文**——采集范围内的文书原文与来源，供逐条回溯。

需要人工确认的条目放在仓库外的私有注记文件里，每条可带一个 `anchor`：命中
表格行或段落的，会以 `※需核实 N` 就地标注在该处，与文末说明逐条对应。注记
文件与采集包同样不得进入仓库。

## 何时不得使用

- 结构化病历未通过校验，或仍有未处理冲突：先回原文核对，不要先做统计。
- 需要正式出具或对外发表：研究数据集是工作副本，不能替代病案首页、正式病历
  或伦理与数据使用审批；对外提供前须按院方政策完成去标识与审批。
- 采集范围未获授权：未确认院方数据边界前，不得对真实患者运行本链路。
- 把归一化结果当诊断依据：`condition` 与 `problem_list` 记录的是文书用词，
  不是编码后的诊断结论；诊断编码（ICD）当前不在本层生成。
