"""求解器：枚举每个接收方的合法闭合视图，再在联合空间中求字典序最优计划。

最优性目标（字典序，依次比较）：
  1. 总成本 = 所有接收方字段签发成本之和 + 模板固定开销 × 不同非空视图集合数；
  2. 发放字段总数（各接收方视图大小之和）；
  3. 计划签名：按接收方 ID 排序的、每个视图内按字段 ID 排序的字段列表序列，
     按字典序取最小。

枚举的正确性论证（README 亦有说明）：
  * 每个接收方的候选集是其允许字段集合中**全部**满足依赖闭包、互斥组、
    证明义务与单视图禁止组合的闭合视图，不做任何支配剪枝——因此零成本
    字段（即使对义务毫无贡献）也保留在候选中，它们可能通过与其它接收方
    拼成完全相同的字段集合来省下一次模板开销，从而改变最优解；
  * 联合搜索在候选笛卡尔积上枚举，并以串阅组的字段并集过滤禁止组合，
    用成本/字段数下界与签名前缀剪枝，剪枝条件均为"严格不可能更优"，
    因此找到的就是全局字典序最优解。
"""
from __future__ import annotations


class Infeasible(Exception):
    """输入合法但不存在可行计划。"""

    def __init__(self, code, details):
        super().__init__(f"{code}: {details}")
        self.code = code
        self.details = details


class View:
    __slots__ = ("mask", "cost", "fields")

    def __init__(self, mask, cost, fields):
        self.mask = mask            # int 位掩码
        self.cost = cost            # 该接收方签发这些字段的成本和
        self.fields = fields        # tuple[str, ...]，已按字段 ID 排序


def _iter_bits(mask):
    while mask:
        lsb = mask & -mask
        yield lsb.bit_length() - 1
        mask ^= lsb


class Engine:
    """预计算：字段索引、依赖传递闭包（支持成环）、强连通分量及其拓扑序。"""

    def __init__(self, case):
        self.case = case
        self.n = len(case.fields)
        self.index = {f.id: i for i, f in enumerate(case.fields)}

        # 传递闭包（含自环）。22 个字段，O(n^3) 足够。
        closure = [1 << i for i in range(self.n)]
        for i, field in enumerate(case.fields):
            for dep in field.depends_on:
                closure[i] |= 1 << self.index[dep]
        for _ in range(self.n):
            for i in range(self.n):
                extra = 0
                for j in _iter_bits(closure[i]):
                    extra |= closure[j]
                closure[i] |= extra
        self.closure = closure

        # 强连通分量：互相可达的字段属于同一分量，代表元为最小下标。
        self.comp_rep = []
        for i in range(self.n):
            rep = i
            for j in range(i):
                if (closure[i] >> j & 1) and (closure[j] >> i & 1):
                    rep = j
                    break
            self.comp_rep.append(rep)
        self.reps = sorted(set(self.comp_rep))
        self.comp_mask = {}
        for rep in self.reps:
            mask = 0
            for i in range(self.n):
                if self.comp_rep[i] == rep:
                    mask |= 1 << i
            self.comp_mask[rep] = mask
        # 同一分量内所有字段的闭包相同，分量闭包即代表元闭包。
        self.comp_closure = {rep: closure[rep] for rep in self.reps}

        # 分量排序：若分量 A 依赖分量 B，则 A 排在 B 之前（依赖者在前）。
        # 对"被依赖者优先"的拓扑序取反即可。
        dependents = {rep: set() for rep in self.reps}   # B -> {依赖 B 的分量}
        indeg = {rep: 0 for rep in self.reps}            # 每个分量的依赖数
        for rep in self.reps:
            seen = set()
            for j in _iter_bits(self.comp_closure[rep]):
                other = self.comp_rep[j]
                if other != rep and other not in seen:
                    seen.add(other)
                    dependents[other].add(rep)
                    indeg[rep] += 1
        ready = sorted(rep for rep in self.reps if indeg[rep] == 0)
        deps_first = []
        while ready:
            rep = ready.pop(0)
            deps_first.append(rep)
            for other in sorted(dependents[rep]):
                indeg[other] -= 1
                if indeg[other] == 0:
                    ready.append(other)
                    ready.sort()
        self.comp_order = list(reversed(deps_first))

        # 互斥组掩码与禁止组合掩码
        group_masks = {}
        for i, field in enumerate(case.fields):
            if field.mutex_group is not None:
                group_masks.setdefault(field.mutex_group, 0)
                group_masks[field.mutex_group] |= 1 << i
        self.mutex_masks = list(group_masks.values())
        self.forbidden_masks = []
        for combo in case.forbidden:
            mask = 0
            for fid in combo:
                mask |= 1 << self.index[fid]
            self.forbidden_masks.append(mask)

    def mask_of(self, field_ids):
        mask = 0
        for fid in field_ids:
            mask |= 1 << self.index[fid]
        return mask

    def closure_of_set(self, mask):
        result = 0
        for i in _iter_bits(mask):
            result |= self.closure[i]
        return result

    def fields_of(self, mask):
        return sorted(self.case.fields[i].id for i in _iter_bits(mask))


def enumerate_views(engine, recipient):
    """枚举某接收方的全部合法闭合视图（View 列表，按成本/字段数/字段序排序）。"""
    case = engine.case
    allowed = engine.mask_of(recipient.allowed_fields)

    # 可发放分量：分量本身及其依赖闭包都落在允许集合内。
    comps = []
    for rep in engine.comp_order:
        if engine.comp_mask[rep] & ~allowed:
            continue
        if engine.comp_closure[rep] & ~allowed:
            continue
        comps.append(rep)
    comp_masks = [engine.comp_mask[rep] for rep in comps]
    comp_closures = [engine.comp_closure[rep] for rep in comps]

    obligations = [
        [engine.mask_of(alt) for alt in obl] for obl in recipient.obligations
    ]
    mutex_masks = engine.mutex_masks
    forbidden_masks = engine.forbidden_masks

    def partial_ok(mask):
        for gm in mutex_masks:
            if (mask & gm).bit_count() > 1:
                return False
        for fm in forbidden_masks:
            if mask & fm == fm:
                return False
        return True

    masks = []

    def rec(k, mask):
        if k == len(comps):
            for obl in obligations:
                if not any(alt & ~mask == 0 for alt in obl):
                    return
            masks.append(mask)
            return
        if comp_masks[k] & mask:
            # 分量已被前序闭包整体带入
            rec(k + 1, mask)
            return
        # 选择 1：不纳入该分量
        rec(k + 1, mask)
        # 选择 2：纳入该分量（连带其依赖闭包）
        new_mask = mask | comp_closures[k]
        if partial_ok(new_mask):
            rec(k + 1, new_mask)

    rec(0, 0)

    views = []
    for mask in masks:
        cost = 0
        for i in _iter_bits(mask):
            cost += case.fields[i].cost_for(recipient.id)
        views.append(View(mask, cost, tuple(engine.fields_of(mask))))
    views.sort(key=lambda v: (v.cost, len(v.fields), v.fields))
    return views


def solve(case):
    """求字典序最优计划。输入不合法抛 StructError，无可行计划抛 Infeasible。"""
    engine = Engine(case)
    recipients = case.recipients  # 已按接收方 ID 排序

    # 义务可覆盖性预检：每条义务至少有一个备选组合的闭包落在允许集合内。
    for recipient in recipients:
        allowed = engine.mask_of(recipient.allowed_fields)
        for oi, obl in enumerate(recipient.obligations):
            coverable = False
            for alt in obl:
                if engine.closure_of_set(engine.mask_of(alt)) & ~allowed == 0:
                    coverable = True
                    break
            if not coverable:
                raise Infeasible("E_OBLIGATION_UNCOVERABLE", {
                    "recipient": recipient.id,
                    "obligation_index": oi,
                    "alternatives": obl,
                })

    candidates = [enumerate_views(engine, r) for r in recipients]
    for recipient, cands in zip(recipients, candidates):
        if not cands:
            raise Infeasible("E_NO_FEASIBLE_VIEW", {"recipient": recipient.id})

    n = len(recipients)
    rid_index = {r.id: i for i, r in enumerate(recipients)}
    groups = [[rid_index[rid] for rid in g] for g in case.co_view_groups]
    groups_of = [[] for _ in range(n)]
    for gi, members in enumerate(groups):
        for mi in members:
            groups_of[mi].append(gi)
    forbidden_masks = engine.forbidden_masks
    template_cost = case.template_cost

    # 后缀下界
    suffix_min_cost = [0] * (n + 1)
    suffix_min_count = [0] * (n + 1)
    suffix_all_empty = [True] * (n + 1)
    for i in range(n - 1, -1, -1):
        suffix_min_cost[i] = suffix_min_cost[i + 1] + candidates[i][0].cost
        suffix_min_count[i] = suffix_min_count[i + 1] + min(
            len(v.fields) for v in candidates[i])
        suffix_all_empty[i] = suffix_all_empty[i + 1] and any(
            v.mask == 0 for v in candidates[i])

    best = {"key": None, "views": None}
    used_templates = {}                 # 非空视图掩码 -> 使用次数
    unions = [0] * len(groups)          # 每个串阅组当前的字段并集
    chosen = [None] * n

    def dfs(i, cost, count, signature):
        if best["key"] is not None:
            best_cost, best_count, best_sig = best["key"]
            template_lb = 0 if (used_templates or suffix_all_empty[i]) \
                else template_cost
            low_cost = cost + suffix_min_cost[i] + template_lb
            if low_cost > best_cost:
                return
            low_count = count + suffix_min_count[i]
            if low_cost == best_cost:
                if low_count > best_count:
                    return
                if low_count == best_count and tuple(signature) > best_sig[:i]:
                    return
        if i == n:
            key = (cost, count, tuple(signature))
            if best["key"] is None or key < best["key"]:
                best["key"] = key
                best["views"] = list(chosen)
            return
        for view in candidates[i]:
            # 串阅约束：并集不得完整包含任何禁止组合
            touched = []
            ok = True
            for gi in groups_of[i]:
                new_union = unions[gi] | view.mask
                for fm in forbidden_masks:
                    if new_union & fm == fm:
                        ok = False
                        break
                if not ok:
                    break
                touched.append((gi, unions[gi]))
                unions[gi] = new_union
            if not ok:
                for gi, old in reversed(touched):
                    unions[gi] = old
                continue
            extra_template = template_cost if (
                view.mask and view.mask not in used_templates) else 0
            if view.mask:
                used_templates[view.mask] = used_templates.get(view.mask, 0) + 1
            chosen[i] = view
            dfs(i + 1, cost + view.cost + extra_template,
                count + len(view.fields), signature + [view.fields])
            chosen[i] = None
            if view.mask:
                used_templates[view.mask] -= 1
                if used_templates[view.mask] == 0:
                    del used_templates[view.mask]
            for gi, old in reversed(touched):
                unions[gi] = old

    dfs(0, 0, 0, [])

    if best["views"] is None:
        raise Infeasible("E_INFEASIBLE_COVIEW", {
            "message": "no assignment of per-recipient views satisfies all "
                       "co-view forbidden combinations",
            "co_view_groups": case.co_view_groups,
        })
    return build_plan(case, best["views"])


def build_plan(case, views):
    """把每个接收方的视图装配成确定性输出计划。"""
    recipients = case.recipients
    issuance_cost = sum(v.cost for v in views)
    field_count = sum(len(v.fields) for v in views)

    distinct = sorted({v.fields for v in views if v.fields})
    templates = []
    for k, field_tuple in enumerate(distinct):
        members = [recipients[i].id for i, v in enumerate(views)
                   if v.fields == field_tuple]
        templates.append({
            "id": f"T{k + 1}",
            "fields": list(field_tuple),
            "recipients": members,
        })
    template_total = case.template_cost * len(templates)

    return {
        "status": "ok",
        "case_id": case.case_id,
        "objective": {
            "issuance_cost": issuance_cost,
            "template_count": len(templates),
            "template_cost": template_total,
            "total_cost": issuance_cost + template_total,
            "field_count": field_count,
        },
        "views": [
            {"recipient": recipients[i].id, "fields": list(views[i].fields)}
            for i in range(len(recipients))
        ],
        "templates": templates,
    }
