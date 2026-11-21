"""字段披露选择器的断言测试。

运行：python -m unittest discover -s tests -v
夹具全部为本地虚构数据，通过临时文件驱动 CLI 主函数。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest

from solution.__main__ import main
from solution.checker import check_plan
from solution.model import StructError, load_case
from solution.solver import Infeasible, solve


def run_cli(argv):
    """运行 CLI，返回 (退出码, 解析后的 stdout JSON)。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    return code, json.loads(buf.getvalue())


class CliCaseMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def write_json(self, name, obj):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        return path

    def plan(self, case, db=False):
        case_path = self.write_json("case.json", case)
        argv = ["plan", case_path]
        if db:
            argv += ["--db", os.path.join(self.dir, "plans.sqlite")]
        return run_cli(argv)


def base_case(**overrides):
    case = {
        "case_id": "t",
        "template_cost": 0,
        "fields": [],
        "recipients": [],
        "co_view_groups": [],
        "forbidden": [],
    }
    case.update(overrides)
    return case


class TestDependencyCycle(CliCaseMixin):
    """依赖成环：披露 detail 必须附 tag，tag 又依赖 detail，闭包为两者。"""

    def case(self):
        return base_case(
            fields=[
                {"id": "detail", "depends_on": ["tag"], "costs": {"*": 1}},
                {"id": "tag", "depends_on": ["detail"], "costs": {"*": 1}},
                {"id": "extra", "costs": {"*": 9}},
            ],
            recipients=[{
                "id": "R1",
                "allowed_fields": ["detail", "tag", "extra"],
                "obligations": [[["detail"]]],
            }],
        )

    def test_cycle_closure_in_plan(self):
        code, out = self.plan(self.case())
        self.assertEqual(code, 0)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["views"], [
            {"recipient": "R1", "fields": ["detail", "tag"]}])
        self.assertEqual(out["objective"]["field_count"], 2)

    def test_check_reports_missing_dependency(self):
        case = load_case(self.case())
        result = check_plan(case, {"views": [
            {"recipient": "R1", "fields": ["detail"]}]})
        self.assertFalse(result["valid"])
        v = result["violation"]
        self.assertEqual(v["code"], "E_DEPENDENCY")
        self.assertEqual(v["field"], "detail")
        self.assertEqual(v["missing"], ["tag"])


class TestCoViewViolation(CliCaseMixin):
    """两个视图分别合规，但串阅组并集完整看到禁止组合。"""

    def case(self):
        return base_case(
            fields=[
                {"id": "a", "costs": {"*": 1}},
                {"id": "b", "costs": {"*": 1}},
                {"id": "c", "costs": {"*": 1}},
            ],
            recipients=[
                {"id": "R1", "allowed_fields": ["a"],
                 "obligations": [[["a"]]]},
                {"id": "R2", "allowed_fields": ["b", "c"],
                 "obligations": [[["b"], ["c"]]]},
            ],
            co_view_groups=[["R1", "R2"]],
            forbidden=[["a", "b"]],
        )

    def test_plan_avoids_coview_union(self):
        code, out = self.plan(self.case())
        self.assertEqual(code, 0)
        views = {v["recipient"]: v["fields"] for v in out["views"]}
        self.assertEqual(views["R1"], ["a"])
        # b 与 a 串阅违规，尽管 b 字典序更小，也只能发 c
        self.assertEqual(views["R2"], ["c"])

    def test_check_flags_coview_with_source(self):
        case = load_case(self.case())
        result = check_plan(case, {"views": [
            {"recipient": "R1", "fields": ["a"]},
            {"recipient": "R2", "fields": ["b"]},
        ]})
        self.assertFalse(result["valid"])
        v = result["violation"]
        self.assertEqual(v["code"], "E_COVIEW_FORBIDDEN")
        self.assertEqual(v["group"], ["R1", "R2"])
        self.assertEqual(v["forbidden"], ["a", "b"])
        self.assertEqual(v["contributors"], {"a": ["R1"], "b": ["R2"]})
        self.assertEqual(v["union"], ["a", "b"])

    def test_each_view_individually_compliant(self):
        case = load_case(self.case())
        # 单独看 R2 的 {b} 视图完全合法（无串阅组时）
        case.co_view_groups = []
        result = check_plan(case, {"views": [
            {"recipient": "R1", "fields": ["a"]},
            {"recipient": "R2", "fields": ["b"]},
        ]})
        self.assertTrue(result["valid"])


class TestSharedTemplateReversal(CliCaseMixin):
    """共用模板反转局部最优：零成本字段通过模板共享改变最优解。"""

    def case(self):
        return base_case(
            template_cost=10,
            fields=[
                {"id": "x", "costs": {"R1": 0, "R2": 4}},
                {"id": "y", "costs": {"*": 0}},
            ],
            recipients=[
                # R1 局部最优是 {x}（成本 0）；y 对 R1 零成本
                {"id": "R1", "allowed_fields": ["x", "y"],
                 "obligations": [[["x"]]]},
                # R2 必须同时拿 x 和 y
                {"id": "R2", "allowed_fields": ["x", "y"],
                 "obligations": [[["x"]], [["y"]]]},
            ],
        )

    def test_zero_cost_field_flips_optimum(self):
        code, out = self.plan(self.case())
        self.assertEqual(code, 0)
        views = {v["recipient"]: v["fields"] for v in out["views"]}
        # 若各自为政：{x} + {x,y} -> 0+4+2*10 = 24
        # 共享模板：{x,y} + {x,y} -> 0+4+1*10 = 14
        self.assertEqual(views["R1"], ["x", "y"])
        self.assertEqual(views["R2"], ["x", "y"])
        self.assertEqual(out["objective"]["total_cost"], 14)
        self.assertEqual(out["objective"]["template_count"], 1)
        self.assertEqual(len(out["templates"]), 1)
        self.assertEqual(out["templates"][0]["recipients"], ["R1", "R2"])

    def test_template_counted_once_for_identical_sets(self):
        code, out = self.plan(self.case())
        self.assertEqual(code, 0)
        self.assertEqual(out["objective"]["template_cost"], 10)


class TestEmptyForbiddenCombo(CliCaseMixin):
    """禁止组合含空组合：结构错误，报告错误码与字段位置。"""

    def test_empty_forbidden_combo(self):
        case = base_case(
            fields=[{"id": "a"}],
            recipients=[{"id": "R1", "allowed_fields": ["a"],
                         "obligations": []}],
            forbidden=[["a"], []],
        )
        code, out = self.plan(case)
        self.assertEqual(code, 2)
        self.assertEqual(out["status"], "error")
        self.assertEqual(out["error"]["code"], "E_FORBIDDEN_EMPTY")
        self.assertEqual(out["error"]["path"], "forbidden[1]")


class TestUncoverableObligation(CliCaseMixin):
    """义务无法覆盖：备选组合的闭包超出允许集合。"""

    def test_uncoverable_obligation(self):
        case = base_case(
            fields=[{"id": "a", "costs": {"*": 1}},
                    {"id": "b", "costs": {"*": 1}}],
            recipients=[{"id": "R1", "allowed_fields": ["a"],
                         "obligations": [[["b"]]]}],
        )
        code, out = self.plan(case)
        self.assertEqual(code, 1)
        self.assertEqual(out["status"], "infeasible")
        self.assertEqual(out["reason"]["code"], "E_OBLIGATION_UNCOVERABLE")
        self.assertEqual(out["reason"]["recipient"], "R1")
        self.assertEqual(out["reason"]["obligation_index"], 0)

    def test_dependency_escape_makes_view_infeasible(self):
        # a 依赖 b，但 b 不在允许集合内，义务又要求 a -> 无任何合法视图
        case = base_case(
            fields=[{"id": "a", "depends_on": ["b"], "costs": {"*": 1}},
                    {"id": "b", "costs": {"*": 1}}],
            recipients=[{"id": "R1", "allowed_fields": ["a"],
                         "obligations": [[["a"]]]}],
        )
        code, out = self.plan(case)
        self.assertEqual(code, 1)
        self.assertEqual(out["status"], "infeasible")
        self.assertEqual(out["reason"]["code"], "E_OBLIGATION_UNCOVERABLE")


class TestMutexGroup(CliCaseMixin):
    def case(self):
        return base_case(
            fields=[
                {"id": "p", "mutex_group": "fin", "costs": {"*": 1}},
                {"id": "q", "mutex_group": "fin", "costs": {"*": 2}},
            ],
            recipients=[{"id": "R1", "allowed_fields": ["p", "q"],
                         "obligations": [[["p"], ["q"]]]}],
        )

    def test_plan_picks_cheaper_of_group(self):
        code, out = self.plan(self.case())
        self.assertEqual(code, 0)
        self.assertEqual(out["views"][0]["fields"], ["p"])

    def test_check_flags_two_in_group(self):
        case = load_case(self.case())
        result = check_plan(case, {"views": [
            {"recipient": "R1", "fields": ["p", "q"]}]})
        self.assertFalse(result["valid"])
        v = result["violation"]
        self.assertEqual(v["code"], "E_MUTEX")
        self.assertEqual(v["group"], "fin")
        self.assertEqual(v["fields"], ["p", "q"])


class TestLexTieBreak(CliCaseMixin):
    """成本与字段数都相同时，按接收方 ID 排序的字段 ID 列表决胜。"""

    def test_lexicographically_smaller_view_wins(self):
        case = base_case(
            fields=[{"id": "b", "costs": {"*": 1}},
                    {"id": "a", "costs": {"*": 1}}],
            recipients=[{"id": "R1", "allowed_fields": ["a", "b"],
                         "obligations": [[["a"], ["b"]]]}],
        )
        code, out = self.plan(case)
        self.assertEqual(code, 0)
        self.assertEqual(out["views"][0]["fields"], ["a"])

    def test_secondary_objective_minimizes_field_count(self):
        case = base_case(
            fields=[{"id": "a", "costs": {"*": 1}},
                    {"id": "z", "costs": {"*": 0}}],
            recipients=[{"id": "R1", "allowed_fields": ["a", "z"],
                         "obligations": [[["a"]]]}],
        )
        code, out = self.plan(case)
        self.assertEqual(code, 0)
        # {a} 与 {a,z} 成本相同（z 零成本），字段数更少者胜
        self.assertEqual(out["views"][0]["fields"], ["a"])
        self.assertEqual(out["objective"]["field_count"], 1)


class TestCheckOrdering(CliCaseMixin):
    """check 返回违反的第一条约束：按接收方 ID 排序、按约束类别顺序。"""

    def test_first_violation_wins(self):
        case = base_case(
            fields=[{"id": "a", "depends_on": ["b"], "costs": {"*": 1}},
                    {"id": "b", "costs": {"*": 1}},
                    {"id": "c", "costs": {"*": 1}}],
            recipients=[
                {"id": "R1", "allowed_fields": ["a", "b"], "obligations": []},
                {"id": "R2", "allowed_fields": ["a", "b", "c"],
                 "obligations": []},
            ],
        )
        loaded = load_case(case)
        # R1 超允许集合（c），R2 缺依赖（a 缺 b）；应先报 R1 的 E_NOT_ALLOWED
        result = check_plan(loaded, {"views": [
            {"recipient": "R2", "fields": ["a"]},
            {"recipient": "R1", "fields": ["c"]},
        ]})
        self.assertFalse(result["valid"])
        v = result["violation"]
        self.assertEqual(v["code"], "E_NOT_ALLOWED")
        self.assertEqual(v["recipient"], "R1")
        self.assertEqual(v["field"], "c")


class TestObligationAlternatives(CliCaseMixin):
    def test_cheapest_covering_alternative_chosen(self):
        case = base_case(
            fields=[{"id": "a", "costs": {"*": 5}},
                    {"id": "b", "costs": {"*": 5}},
                    {"id": "c", "costs": {"*": 1}}],
            recipients=[{"id": "R1", "allowed_fields": ["a", "b", "c"],
                         "obligations": [[["a", "b"], ["c"]]]}],
        )
        code, out = self.plan(case)
        self.assertEqual(code, 0)
        self.assertEqual(out["views"][0]["fields"], ["c"])


class TestPersistence(CliCaseMixin):
    """涉及持久状态的命令带 --db：plan 入库，check 出库校验。"""

    def case(self):
        return base_case(
            template_cost=3,
            fields=[{"id": "a", "costs": {"*": 1}},
                    {"id": "b", "costs": {"*": 1}}],
            recipients=[
                {"id": "R1", "allowed_fields": ["a", "b"],
                 "obligations": [[["a"]]]},
                {"id": "R2", "allowed_fields": ["a", "b"],
                 "obligations": [[["b"]]]},
            ],
        )

    def test_plan_then_check_via_db(self):
        case_path = self.write_json("case.json", self.case())
        db_path = os.path.join(self.dir, "plans.sqlite")
        code, plan_out = run_cli(["plan", case_path, "--db", db_path])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(db_path))
        code, check_out = run_cli(["check", case_path, "--db", db_path])
        self.assertEqual(code, 0)
        self.assertTrue(check_out["valid"])
        self.assertEqual(check_out["checked_views"], 2)

    def test_check_without_stored_plan(self):
        case_path = self.write_json("case.json", self.case())
        db_path = os.path.join(self.dir, "empty.sqlite")
        code, out = run_cli(["check", case_path, "--db", db_path])
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_NO_STORED_PLAN")

    def test_check_plan_file(self):
        case_path = self.write_json("case.json", self.case())
        plan_path = self.write_json("plan.json", {"views": [
            {"recipient": "R1", "fields": ["a"]},
            {"recipient": "R2", "fields": ["b"]},
        ]})
        code, out = run_cli(["check", case_path, "--plan", plan_path])
        self.assertEqual(code, 0)
        self.assertTrue(out["valid"])

    def test_check_detects_objective_mismatch(self):
        case_path = self.write_json("case.json", self.case())
        plan_path = self.write_json("plan.json", {
            "views": [
                {"recipient": "R1", "fields": ["a"]},
                {"recipient": "R2", "fields": ["b"]},
            ],
            "objective": {"total_cost": 999},
        })
        code, out = run_cli(["check", case_path, "--plan", plan_path])
        self.assertEqual(code, 1)
        self.assertEqual(out["violation"]["code"], "E_OBJECTIVE_MISMATCH")


class TestStructuralErrors(CliCaseMixin):
    def test_unknown_field_reference(self):
        case = base_case(
            fields=[{"id": "a"}],
            recipients=[{"id": "R1", "allowed_fields": ["a", "ghost"],
                         "obligations": []}],
        )
        code, out = self.plan(case)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_UNKNOWN_FIELD")
        self.assertEqual(out["error"]["path"],
                         "recipients[0].allowed_fields")

    def test_duplicate_field_id(self):
        case = base_case(
            fields=[{"id": "a"}, {"id": "a"}],
            recipients=[{"id": "R1", "allowed_fields": [], "obligations": []}],
        )
        code, out = self.plan(case)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_DUPLICATE")
        self.assertEqual(out["error"]["path"], "fields[1].id")

    def test_unknown_recipient_in_coview_group(self):
        case = base_case(
            fields=[{"id": "a"}],
            recipients=[{"id": "R1", "allowed_fields": ["a"],
                         "obligations": []}],
            co_view_groups=[["R1", "R9"]],
        )
        code, out = self.plan(case)
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_UNKNOWN_RECIPIENT")
        self.assertEqual(out["error"]["path"], "co_view_groups[0]")

    def test_invalid_json_file(self):
        path = os.path.join(self.dir, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        code, out = run_cli(["plan", path])
        self.assertEqual(code, 2)
        self.assertEqual(out["error"]["code"], "E_JSON")


class TestDeterminism(CliCaseMixin):
    """同一逻辑输入的决定一致。"""

    def test_repeated_runs_identical(self):
        case = base_case(
            template_cost=4,
            fields=[
                {"id": "a", "depends_on": ["b"], "costs": {"*": 1}},
                {"id": "b", "costs": {"R1": 2, "R2": 1}},
                {"id": "c", "mutex_group": "g", "costs": {"*": 1}},
                {"id": "d", "mutex_group": "g", "costs": {"*": 1}},
            ],
            recipients=[
                {"id": "R1", "allowed_fields": ["a", "b", "c", "d"],
                 "obligations": [[["a"]], [["c"], ["d"]]]},
                {"id": "R2", "allowed_fields": ["b", "c", "d"],
                 "obligations": [[["b"], ["c"]]]},
            ],
            co_view_groups=[["R1", "R2"]],
            forbidden=[["a", "c"]],
        )
        code1, out1 = self.plan(case)
        code2, out2 = self.plan(case)
        self.assertEqual(code1, code2)
        self.assertEqual(out1, out2)
        self.assertEqual(code1, 0)


class TestScale(CliCaseMixin):
    """8 个接收方、22 个字段的冒烟测试。"""

    def test_eight_recipients_twentytwo_fields(self):
        fields = []
        for k in range(22):
            fid = f"f{k:02d}"
            spec = {"id": fid, "costs": {"*": (k % 3)}}
            if k > 0 and k % 4 == 0:
                spec["depends_on"] = [f"f{k - 1:02d}"]
            if k in (10, 11, 12):
                spec["mutex_group"] = "trio"
            fields.append(spec)
        recipients = []
        for r in range(8):
            allowed = [f"f{k:02d}" for k in range(22) if (k + r) % 2 == 0]
            need = allowed[0] if allowed else "f00"
            recipients.append({
                "id": f"R{r}",
                "allowed_fields": allowed,
                "obligations": [[[need]]],
            })
        case = base_case(template_cost=2, fields=fields,
                         recipients=recipients,
                         co_view_groups=[["R0", "R1"], ["R2", "R3", "R4"]],
                         forbidden=[["f10", "f11"]])
        code, out = self.plan(case)
        self.assertEqual(code, 0)
        self.assertEqual(len(out["views"]), 8)
        # 计划自身必须通过 check
        case_path = self.write_json("case.json", case)
        plan_path = self.write_json("plan.json", out)
        code, check_out = run_cli(["check", case_path, "--plan", plan_path])
        self.assertEqual(code, 0)
        self.assertTrue(check_out["valid"])


if __name__ == "__main__":
    unittest.main()
