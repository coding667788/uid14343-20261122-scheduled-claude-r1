"""输入模型与结构校验。

结构错误一律抛出 StructError，携带错误码（code）与具体字段位置（path），
例如 ``recipients[0].obligations[1][0]``。
"""
from __future__ import annotations


class StructError(Exception):
    """结构错误：携带错误码与具体字段位置。"""

    def __init__(self, code, path, message):
        super().__init__(f"{code} at {path}: {message}")
        self.code = code
        self.path = path
        self.message = message

    def to_dict(self):
        return {"code": self.code, "path": self.path, "message": self.message}


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require(cond, code, path, message):
    if not cond:
        raise StructError(code, path, message)


class Field:
    __slots__ = ("id", "depends_on", "mutex_group", "costs")

    def __init__(self, field_id, depends_on, mutex_group, costs):
        self.id = field_id
        self.depends_on = depends_on          # list[str]，披露本字段必须同时披露的字段
        self.mutex_group = mutex_group        # str | None，互斥组名
        self.costs = costs                    # dict[str, number]，"*" 为默认签发成本

    def cost_for(self, recipient_id):
        """签发本字段给指定接收方的成本；缺省回退到 "*"，再缺省为 0。"""
        if recipient_id in self.costs:
            return self.costs[recipient_id]
        return self.costs.get("*", 0)


class Recipient:
    __slots__ = ("id", "allowed_fields", "obligations")

    def __init__(self, recipient_id, allowed_fields, obligations):
        self.id = recipient_id
        self.allowed_fields = allowed_fields  # list[str]
        # obligations: list[obligation]；obligation: list[alternative]；alternative: list[str]
        self.obligations = obligations


class Case:
    __slots__ = (
        "case_id", "template_cost", "fields", "recipients",
        "co_view_groups", "forbidden",
    )

    def __init__(self, case_id, template_cost, fields, recipients,
                 co_view_groups, forbidden):
        self.case_id = case_id
        self.template_cost = template_cost
        self.fields = fields                  # list[Field]，保持输入顺序
        self.recipients = recipients          # list[Recipient]，按接收方 ID 排序
        self.co_view_groups = co_view_groups  # list[list[str]]，串阅组（接收方 ID）
        self.forbidden = forbidden            # list[list[str]]，禁止组合（已排序去重）


def load_case(obj):
    """把 JSON 对象校验并转换为 Case；结构错误抛 StructError。"""
    _require(isinstance(obj, dict), "E_SCHEMA", "$", "case must be a JSON object")

    case_id = obj.get("case_id", "")
    _require(isinstance(case_id, str), "E_SCHEMA", "case_id",
             "case_id must be a string")

    template_cost = obj.get("template_cost", 0)
    _require(_is_number(template_cost) and template_cost >= 0,
             "E_BAD_VALUE", "template_cost",
             "template_cost must be a non-negative number")

    # ---- fields ----
    raw_fields = obj.get("fields")
    _require(isinstance(raw_fields, list), "E_SCHEMA", "fields",
             "fields must be a list")
    fields = []
    field_ids = set()
    for i, raw in enumerate(raw_fields):
        path = f"fields[{i}]"
        _require(isinstance(raw, dict), "E_SCHEMA", path,
                 "field must be an object")
        fid = raw.get("id")
        _require(isinstance(fid, str) and fid, "E_SCHEMA", f"{path}.id",
                 "field id must be a non-empty string")
        _require(fid not in field_ids, "E_DUPLICATE", f"{path}.id",
                 f"duplicate field id {fid!r}")
        field_ids.add(fid)
        deps = raw.get("depends_on", [])
        _require(isinstance(deps, list)
                 and all(isinstance(d, str) for d in deps),
                 "E_SCHEMA", f"{path}.depends_on",
                 "depends_on must be a list of field ids")
        mutex_group = raw.get("mutex_group", None)
        _require(mutex_group is None or isinstance(mutex_group, str),
                 "E_SCHEMA", f"{path}.mutex_group",
                 "mutex_group must be a string or null")
        costs = raw.get("costs", {})
        _require(isinstance(costs, dict), "E_SCHEMA", f"{path}.costs",
                 "costs must be an object mapping recipient id (or '*') to a number")
        for key, val in costs.items():
            _require(isinstance(key, str) and key, "E_SCHEMA",
                     f"{path}.costs", "cost keys must be non-empty strings")
            _require(_is_number(val) and val >= 0, "E_BAD_VALUE",
                     f"{path}.costs.{key}", "cost must be a non-negative number")
        fields.append(Field(fid, list(deps), mutex_group, dict(costs)))

    for i, field in enumerate(fields):
        for dep in field.depends_on:
            _require(dep in field_ids, "E_UNKNOWN_FIELD",
                     f"fields[{i}].depends_on", f"unknown field {dep!r}")

    # ---- recipients ----
    raw_recipients = obj.get("recipients")
    _require(isinstance(raw_recipients, list) and raw_recipients,
             "E_SCHEMA", "recipients", "recipients must be a non-empty list")
    recipients = []
    recipient_ids = set()
    for i, raw in enumerate(raw_recipients):
        path = f"recipients[{i}]"
        _require(isinstance(raw, dict), "E_SCHEMA", path,
                 "recipient must be an object")
        rid = raw.get("id")
        _require(isinstance(rid, str) and rid, "E_SCHEMA", f"{path}.id",
                 "recipient id must be a non-empty string")
        _require(rid not in recipient_ids, "E_DUPLICATE", f"{path}.id",
                 f"duplicate recipient id {rid!r}")
        recipient_ids.add(rid)
        allowed = raw.get("allowed_fields", [])
        _require(isinstance(allowed, list)
                 and all(isinstance(a, str) for a in allowed),
                 "E_SCHEMA", f"{path}.allowed_fields",
                 "allowed_fields must be a list of field ids")
        _require(len(set(allowed)) == len(allowed), "E_DUPLICATE",
                 f"{path}.allowed_fields", "duplicate field in allowed_fields")
        for a in allowed:
            _require(a in field_ids, "E_UNKNOWN_FIELD",
                     f"{path}.allowed_fields", f"unknown field {a!r}")
        obligations = raw.get("obligations", [])
        _require(isinstance(obligations, list), "E_SCHEMA",
                 f"{path}.obligations", "obligations must be a list")
        norm_obligations = []
        for oi, obl in enumerate(obligations):
            opath = f"{path}.obligations[{oi}]"
            _require(isinstance(obl, list), "E_SCHEMA", opath,
                     "obligation must be a list of alternative combinations")
            norm_obl = []
            for ai, alt in enumerate(obl):
                apath = f"{opath}[{ai}]"
                _require(isinstance(alt, list)
                         and all(isinstance(x, str) for x in alt),
                         "E_SCHEMA", apath,
                         "alternative must be a list of field ids")
                for x in alt:
                    _require(x in field_ids, "E_UNKNOWN_FIELD", apath,
                             f"unknown field {x!r}")
                norm_obl.append(list(alt))
            norm_obligations.append(norm_obl)
        recipients.append(Recipient(rid, list(allowed), norm_obligations))
    recipients.sort(key=lambda r: r.id)

    # ---- co_view_groups ----
    co_view_groups = obj.get("co_view_groups", [])
    _require(isinstance(co_view_groups, list), "E_SCHEMA", "co_view_groups",
             "co_view_groups must be a list")
    norm_groups = []
    for gi, group in enumerate(co_view_groups):
        gpath = f"co_view_groups[{gi}]"
        _require(isinstance(group, list) and len(group) >= 2, "E_SCHEMA",
                 gpath, "co-view group must list at least two recipient ids")
        _require(all(isinstance(r, str) for r in group), "E_SCHEMA", gpath,
                 "co-view group members must be recipient id strings")
        _require(len(set(group)) == len(group), "E_DUPLICATE", gpath,
                 "duplicate recipient in co-view group")
        for rid in group:
            _require(rid in recipient_ids, "E_UNKNOWN_RECIPIENT", gpath,
                     f"unknown recipient {rid!r}")
        norm_groups.append(list(group))

    # ---- forbidden ----
    forbidden = obj.get("forbidden", [])
    _require(isinstance(forbidden, list), "E_SCHEMA", "forbidden",
             "forbidden must be a list")
    norm_forbidden = []
    for fi, combo in enumerate(forbidden):
        fpath = f"forbidden[{fi}]"
        _require(isinstance(combo, list), "E_SCHEMA", fpath,
                 "forbidden combination must be a list of field ids")
        _require(len(combo) > 0, "E_FORBIDDEN_EMPTY", fpath,
                 "forbidden combination must not be empty")
        for x in combo:
            _require(isinstance(x, str), "E_SCHEMA", fpath,
                     "forbidden combination entries must be field id strings")
            _require(x in field_ids, "E_UNKNOWN_FIELD", fpath,
                     f"unknown field {x!r}")
        norm_forbidden.append(sorted(set(combo)))

    return Case(case_id, template_cost, fields, recipients,
                norm_groups, norm_forbidden)
