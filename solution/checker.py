"""计划校验器：对给定计划返回违反的第一条约束。

检查顺序（即"第一条"的定义，README 亦有说明）：
  1. 计划结构（views 列表及其元素形态）；
  2. 接收方覆盖（每个接收方恰好一个视图，无多余接收方）；
  3. 按接收方 ID 排序逐方检查：未知字段 -> 超允许集合 -> 依赖闭包 ->
     互斥组 -> 证明义务 -> 单视图禁止组合；
  4. 按输入顺序逐串阅组检查：成员视图并集不得完整包含禁止组合
     （违规时附串阅来源：组成员、并集、各禁止字段的贡献者）；
  5. 若计划声明了 objective，则与按视图重算的目标值比对。
"""
from __future__ import annotations

from .solver import Engine, View, build_plan


def _violation(code, message, **details):
    return {"valid": False,
            "violation": {"code": code, "message": message, **details}}


def check_plan(case, plan):
    """校验计划，返回 {"valid": True, ...} 或 {"valid": False, "violation": {...}}。"""
    if not isinstance(plan, dict) or not isinstance(plan.get("views"), list):
        return _violation("E_PLAN_SCHEMA",
                          "plan must be an object with a 'views' list")

    # 1. 计划结构
    by_recipient = {}
    for vi, view in enumerate(plan["views"]):
        path = f"views[{vi}]"
        if (not isinstance(view, dict)
                or not isinstance(view.get("recipient"), str)
                or not isinstance(view.get("fields"), list)
                or not all(isinstance(x, str) for x in view["fields"])):
            return _violation(
                "E_PLAN_SCHEMA",
                "each view must be an object with string 'recipient' and a "
                "list of field id strings in 'fields'", path=path)
        rid = view["recipient"]
        if rid in by_recipient:
            return _violation("E_PLAN_RECIPIENT",
                              f"duplicate view for recipient {rid!r}",
                              recipient=rid)
        if len(set(view["fields"])) != len(view["fields"]):
            return _violation("E_PLAN_SCHEMA",
                              f"duplicate field in view of recipient {rid!r}",
                              recipient=rid, path=path)
        by_recipient[rid] = list(view["fields"])

    # 2. 接收方覆盖
    expected_ids = [r.id for r in case.recipients]
    for rid in expected_ids:
        if rid not in by_recipient:
            return _violation("E_PLAN_RECIPIENT",
                              f"missing view for recipient {rid!r}",
                              recipient=rid)
    known = set(expected_ids)
    for rid in sorted(by_recipient):
        if rid not in known:
            return _violation("E_PLAN_RECIPIENT",
                              f"unexpected recipient {rid!r}", recipient=rid)

    engine = Engine(case)
    masks = {}

    # 3. 逐接收方检查（按接收方 ID 排序）
    for recipient in case.recipients:
        rid = recipient.id
        field_list = by_recipient[rid]
        for fid in field_list:
            if fid not in engine.index:
                return _violation("E_UNKNOWN_FIELD",
                                  f"unknown field {fid!r} in view of {rid!r}",
                                  recipient=rid, field=fid)
        mask = engine.mask_of(field_list)
        masks[rid] = mask
        allowed = engine.mask_of(recipient.allowed_fields)
        for fid in field_list:
            if not (allowed >> engine.index[fid]) & 1:
                return _violation("E_NOT_ALLOWED",
                                  f"field {fid!r} is not in the allowed set "
                                  f"of recipient {rid!r}",
                                  recipient=rid, field=fid)
        for fid in field_list:
            missing_mask = engine.closure[engine.index[fid]] & ~mask
            if missing_mask:
                return _violation(
                    "E_DEPENDENCY",
                    f"field {fid!r} requires additional fields in the view "
                    f"of {rid!r}",
                    recipient=rid, field=fid,
                    missing=engine.fields_of(missing_mask))
        group_fields = {}
        for fid in field_list:
            group = case.fields[engine.index[fid]].mutex_group
            if group is not None:
                group_fields.setdefault(group, []).append(fid)
        for group in sorted(group_fields):
            members = sorted(group_fields[group])
            if len(members) > 1:
                return _violation(
                    "E_MUTEX",
                    f"mutex group {group!r} allows at most one field in the "
                    f"view of {rid!r}",
                    recipient=rid, group=group, fields=members)
        for oi, obl in enumerate(recipient.obligations):
            if not any(set(alt) <= set(field_list) for alt in obl):
                return _violation(
                    "E_OBLIGATION",
                    f"obligation {oi} of recipient {rid!r} is not satisfied",
                    recipient=rid, obligation_index=oi, alternatives=obl)
        for combo in case.forbidden:
            if set(combo) <= set(field_list):
                return _violation(
                    "E_FORBIDDEN",
                    f"view of {rid!r} fully contains a forbidden combination",
                    recipient=rid, forbidden=combo)

    # 4. 串阅组检查（附串阅来源）
    for group in case.co_view_groups:
        union = set()
        for rid in group:
            union.update(by_recipient[rid])
        for combo in case.forbidden:
            if set(combo) <= union:
                contributors = {
                    fid: sorted(rid for rid in group
                                if fid in by_recipient[rid])
                    for fid in combo
                }
                return _violation(
                    "E_COVIEW_FORBIDDEN",
                    "co-view group jointly sees a forbidden combination",
                    group=list(group), forbidden=combo,
                    union=sorted(union), contributors=contributors)

    # 5. 目标值一致性（若计划声明了 objective）
    if "objective" in plan:
        views_for_build = []
        for recipient in case.recipients:
            mask = masks[recipient.id]
            cost = sum(case.fields[i].cost_for(recipient.id)
                       for i in range(engine.n) if (mask >> i) & 1)
            views_for_build.append(
                View(mask, cost, tuple(engine.fields_of(mask))))
        recomputed = build_plan(case, views_for_build)["objective"]
        declared = plan["objective"]
        if not isinstance(declared, dict):
            return _violation("E_PLAN_SCHEMA",
                              "objective must be an object when present")
        mismatched = {}
        for key in ("issuance_cost", "template_count", "template_cost",
                    "total_cost", "field_count"):
            if key in declared and declared[key] != recomputed[key]:
                mismatched[key] = {"declared": declared[key],
                                   "recomputed": recomputed[key]}
        if mismatched:
            return _violation("E_OBJECTIVE_MISMATCH",
                              "declared objective does not match the views",
                              mismatched=mismatched,
                              recomputed=recomputed)

    return {"valid": True, "checked_views": len(plan["views"])}
