# 字段披露选择器（跨法域审计请求的公共字段最小封装）

为内部合成记录规划并校验各接收方的披露视图：每个接收方声明允许字段集合与
证明义务，字段之间存在依赖（可成环，需取闭包）与互斥组，某些字段组合禁止
被同一接收方或任一串阅组完整看到；签发字段按（字段, 接收方）计费，每种不同
的封装模板（完全相同的字段集合）另收一次固定开销。

所有法域、接收方与字段均为虚构，全部规则完全取自输入文件。实现仅使用
Windows 原生 Python 3.12 标准库与 CPU，无第三方依赖。

## 运行方式

在仓库根目录执行：

```bash
# 求最优披露计划（结果打印到 stdout）
python -m solution plan examples/case_demo.json

# 求最优计划并入库（涉及持久状态，需带 --db）
python -m solution plan examples/case_demo.json --db demo.sqlite

# 校验库中该用例最近一次存入的计划
python -m solution check examples/case_demo.json --db demo.sqlite

# 校验一个计划文件（无持久状态，不需要 --db）
python -m solution check examples/case_coview.json --plan examples/plan_bad_coview.json

# 运行全部断言测试
python -m unittest discover -s tests -v
```

标准输出一律为 UTF-8 JSON。退出码：

| 退出码 | 含义 |
|---|---|
| 0 | 成功（plan 找到计划 / check 校验通过） |
| 1 | plan 无可行计划 / check 发现约束违反 |
| 2 | 结构错误或用法错误（输出含错误码与具体字段位置） |

同一逻辑输入的决定一致：接收方按 ID 排序处理，视图内字段按字段 ID 排序，
模板按字段集合排序编号（T1、T2、…），所有决胜规则均为确定性字典序。

## 输入定义（CASE.json）

```json
{
  "case_id": "demo-2026-11",
  "template_cost": 6,
  "fields": [
    {
      "id": "detail_status",
      "depends_on": ["category_tag"],
      "mutex_group": null,
      "costs": {"*": 2, "AUD-East": 1}
    }
  ],
  "recipients": [
    {
      "id": "AUD-East",
      "allowed_fields": ["detail_status", "category_tag"],
      "obligations": [[["detail_status"], ["category_tag", "region_code"]]]
    }
  ],
  "co_view_groups": [["AUD-East", "AUD-West"]],
  "forbidden": [["subject_home_addr", "subject_salary_band"]]
}
```

| 键 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `case_id` | string | `""` | 用例标识，原样回显 |
| `template_cost` | number ≥ 0 | `0` | 每种不同非空视图集合的一次性封装模板开销 |
| `fields` | list | 必填 | 字段定义列表 |
| `fields[].id` | string | 必填 | 字段 ID，全局唯一 |
| `fields[].depends_on` | list[string] | `[]` | 披露本字段必须同时披露的字段；依赖可成环，取传递闭包 |
| `fields[].mutex_group` | string \| null | `null` | 互斥组名；同一视图内每组至多一个字段 |
| `fields[].costs` | object | `{}` | 签发成本表：键为接收方 ID，`"*"` 为默认值；查表顺序为接收方 ID → `"*"` → 0 |
| `recipients` | list | 必填 | 接收方列表，ID 全局唯一 |
| `recipients[].allowed_fields` | list[string] | `[]` | 允许发放给该接收方的字段集合 |
| `recipients[].obligations` | list | `[]` | 证明义务列表；每条义务是若干备选字段组合，任一组合被视图完整包含即满足 |
| `co_view_groups` | list[list[string]] | `[]` | 可能串阅的接收方组（≥2 方）；组看到成员视图的字段并集 |
| `forbidden` | list[list[string]] | `[]` | 禁止组合；任何单一视图或任一串阅组并集都不得完整包含之（单个字段本身可合法）。空组合是结构错误 |

未识别的顶层/对象键被忽略。义务中的空备选组合 `[]` 表示该义务被任意视图
满足；空义务（无备选组合）永远无法满足，会导致不可行。

## 输出定义

### plan 成功（退出码 0）

```json
{
  "status": "ok",
  "case_id": "demo-2026-11",
  "objective": {
    "issuance_cost": 8,
    "template_count": 2,
    "template_cost": 12,
    "total_cost": 20,
    "field_count": 7
  },
  "views": [
    {"recipient": "AUD-East", "fields": ["budget_extract", "category_tag", "detail_status"]}
  ],
  "templates": [
    {"id": "T1", "fields": ["..."], "recipients": ["AUD-East"]}
  ]
}
```

* `views`：按接收方 ID 排序；`fields` 按字段 ID 排序。
* `templates`：每种不同的非空字段集合一个模板，按字段列表排序编号；
  同一集合被多个接收方共用只收一次固定开销（空视图不产生模板）。
* `objective.total_cost = issuance_cost + template_cost`，其中
  `template_cost = 输入 template_cost × template_count`。

### plan 不可行（退出码 1）

```json
{"status": "infeasible", "reason": {"code": "E_OBLIGATION_UNCOVERABLE", "recipient": "R1", "obligation_index": 0, "alternatives": [["b"]]}}
```

`reason.code` 取值：

| 错误码 | 含义 |
|---|---|
| `E_OBLIGATION_UNCOVERABLE` | 某条义务的所有备选组合的依赖闭包都超出该接收方允许集合 |
| `E_NO_FEASIBLE_VIEW` | 该接收方不存在任何满足闭包/互斥/义务/禁止组合的视图 |
| `E_INFEASIBLE_COVIEW` | 各方单独均可行，但串阅约束下不存在联合可行分配 |

### check（退出码 0 通过 / 1 违反）

通过：`{"status": "ok", "valid": true, "checked_views": 3}`

违反：返回**违反的第一条约束**，串阅违规附串阅来源：

```json
{
  "status": "ok",
  "valid": false,
  "violation": {
    "code": "E_COVIEW_FORBIDDEN",
    "message": "co-view group jointly sees a forbidden combination",
    "group": ["R1", "R2"],
    "forbidden": ["alpha", "beta"],
    "union": ["alpha", "beta"],
    "contributors": {"alpha": ["R1"], "beta": ["R2"]}
  }
}
```

`contributors` 标明禁止组合中每个字段由组内哪些接收方的视图贡献，即串阅来源。

"第一条"的检查顺序固定为：

1. 计划结构（`views` 列表形态、字段去重）；
2. 接收方覆盖（每个接收方恰好一个视图、无多余接收方）；
3. 按接收方 ID 排序逐方检查：未知字段 → 超允许集合 → 依赖闭包 →
   互斥组 → 证明义务 → 单视图禁止组合；
4. 按输入顺序逐串阅组检查并集禁止组合；
5. 若计划声明了 `objective`，与按视图重算的目标值比对。

check 的计划输入可以是 plan 的完整输出，也可以是只含 `views` 的对象。

### 结构错误（退出码 2）

```json
{"status": "error", "error": {"code": "E_FORBIDDEN_EMPTY", "path": "forbidden[1]", "message": "forbidden combination must not be empty"}}
```

| 错误码 | 含义 |
|---|---|
| `E_SCHEMA` | 类型/形状不符，`path` 给出具体字段位置（如 `recipients[0].obligations[1][0]`） |
| `E_UNKNOWN_FIELD` / `E_UNKNOWN_RECIPIENT` | 引用了未定义的字段/接收方 |
| `E_DUPLICATE` | 字段、接收方或组内成员重复 |
| `E_BAD_VALUE` | 成本为负数等取值错误 |
| `E_FORBIDDEN_EMPTY` | 禁止组合为空组合 |
| `E_JSON` / `E_IO` | 文件不是合法 JSON / 无法读取 |
| `E_USAGE` / `E_NO_STORED_PLAN` | 命令参数缺失或冲突 / 库中无该用例的计划 |

## 最优性目标与求解方法

### 目标（字典序，依次比较）

1. **总成本最小** = 所有接收方字段签发成本之和 + `template_cost` × 不同非空
   视图集合数；
2. **发放字段总数最小**（各视图大小之和）；
3. **计划签名最小**：按接收方 ID 排序的、每个视图内按字段 ID 排序的字段
   列表序列，按字典序取最小。

### 合法闭合视图的枚举

对每个接收方独立枚举其**全部**合法视图：

1. 对依赖图取传递闭包并按强连通分量收缩——依赖成环的分量要么整体进入
   视图、要么整体不进入，闭包自然收敛；
2. 分量按"依赖者在前"的拓扑序排列后递归枚举：每个分量只有"不纳入"与
   "纳入（连带其依赖闭包）"两种选择，后续分量若已被闭包带入则跳过，
   每个闭合视图恰好生成一次；
3. 枚举途中按互斥组与单视图禁止组合剪枝（二者对超集单调，剪掉安全）；
4. 叶子处校验每条证明义务至少被一个备选组合满足；
5. 分量可发放的前提是其自身及依赖闭包都落在该接收方的允许集合内。

候选集**不做任何支配剪枝**（例如"成本更高且字段更多"的视图也保留）。
这正是零成本字段能改变最优解的原因：一个对义务毫无贡献的零成本字段，
可能把本接收方的视图补齐成与另一接收方**完全相同的字段集合**，从而共用
一个模板、省下一次 `template_cost`。若按局部成本支配关系剪掉这些视图，
就会丢失这类全局最优。联合搜索在每个接收方的完整候选集之笛卡尔积上进行，
用串阅组的字段并集过滤禁止组合，并以如下安全下界剪枝：

* 成本下界 = 已发生成本 + 剩余接收方各自最小视图成本之和 + 模板下界
  （尚未产生任何模板且剩余接收方无法全取空视图时至少再加一次 `template_cost`）；
* 字段数下界同理；
* 签名前缀已大于当前最优签名前缀的分支剪除。

所有剪枝条件都是"严格不可能更优"，因此最终结果就是全局字典序最优计划。
规模上支持 8 个接收方、22 个字段（见 `tests/test_solution.py` 的
`TestScale`）；枚举复杂度取决于闭合视图的数量，允许集合、义务、互斥与
禁止组合都会显著收缩搜索空间。

## 样例复现

```bash
# 1) 演示用例：依赖闭包 + 互斥组 + 模板共享（AUD-West 与 REG-North 共用 T2）
python -m solution plan examples/case_demo.json --db demo.sqlite
python -m solution check examples/case_demo.json --db demo.sqlite
#    -> {"status": "ok", "valid": true, "checked_views": 3}

# 2) 串阅违规：两个视图各自合规，并集完整看到禁止组合
python -m solution check examples/case_coview.json --plan examples/plan_bad_coview.json
#    -> valid=false, violation.code=E_COVIEW_FORBIDDEN（附 group 与 contributors）

# 3) 同一用例的最优计划会避开串阅并集（R2 改发 gamma）
python -m solution plan examples/case_coview.json
```

## 仓库结构

```
solution/
  __main__.py   # CLI：plan / check，UTF-8 JSON 输出，退出码约定
  model.py      # 输入模型与结构校验（错误码 + 字段位置）
  solver.py     # 闭包枚举 + 联合字典序最优搜索
  checker.py    # 计划校验：返回违反的第一条约束并附串阅来源
  store.py      # SQLite 持久化（--db）
examples/       # 可直接运行的虚构样例
tests/          # unittest 断言测试（python -m unittest discover -s tests -v）
```
