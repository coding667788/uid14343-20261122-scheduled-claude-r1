"""命令行入口：``python -m solution <命令> CASE.json``。

命令：
  plan CASE.json [--db DB.sqlite]   求最优披露计划；带 --db 时把计划入库
  check CASE.json (--plan PLAN.json | --db DB.sqlite)
                                    校验计划文件，或校验库中该用例最新计划

标准输出为 UTF-8 JSON；退出码：
  0  成功（plan 找到计划 / check 校验通过）
  1  plan 无可行计划 / check 发现约束违反
  2  结构错误或用法错误（输出含错误码与具体字段位置）
"""
from __future__ import annotations

import argparse
import json
import sys

from . import store
from .checker import check_plan
from .model import StructError, load_case
from .solver import Infeasible, solve


def _emit(obj):
    text = json.dumps(obj, ensure_ascii=False, indent=2)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise StructError("E_IO", path, "file not found")
    except OSError as exc:
        raise StructError("E_IO", path, str(exc))
    except json.JSONDecodeError as exc:
        raise StructError("E_JSON", f"{path}:{exc.lineno}:{exc.colno}",
                          f"invalid JSON: {exc.msg}")


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m solution",
        description="字段披露选择器：规划并校验各接收方的披露视图")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="求最优披露计划")
    p_plan.add_argument("case", help="用例 JSON 文件路径")
    p_plan.add_argument("--db", help="SQLite 数据库路径；提供时把计划入库")

    p_check = sub.add_parser("check", help="校验给定计划")
    p_check.add_argument("case", help="用例 JSON 文件路径")
    p_check.add_argument("--plan", help="计划 JSON 文件路径")
    p_check.add_argument("--db", help="SQLite 数据库路径；取该用例最新计划")
    return parser


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _build_parser().parse_args(argv)
    try:
        case_obj = _read_json(args.case)
        case = load_case(case_obj)
        if args.command == "plan":
            return _cmd_plan(args, case_obj, case)
        return _cmd_check(args, case_obj, case)
    except StructError as exc:
        _emit({"status": "error", "error": exc.to_dict()})
        return 2


def _cmd_plan(args, case_obj, case):
    try:
        plan = solve(case)
    except Infeasible as exc:
        result = {"status": "infeasible",
                  "reason": {"code": exc.code, **exc.details}}
        if args.db:
            store.save_plan(args.db, case_obj, result)
        _emit(result)
        return 1
    if args.db:
        store.save_plan(args.db, case_obj, plan)
    _emit(plan)
    return 0


def _cmd_check(args, case_obj, case):
    if args.plan and args.db:
        raise StructError("E_USAGE", "--plan/--db",
                          "use either --plan or --db, not both")
    if args.plan:
        plan = _read_json(args.plan)
    elif args.db:
        plan = store.load_latest_plan(args.db, case_obj)
        if plan is None:
            raise StructError("E_NO_STORED_PLAN", args.db,
                              "no stored plan for this case in the database")
    else:
        raise StructError("E_USAGE", "check",
                          "check requires --plan PLAN.json or --db DB.sqlite")
    result = check_plan(case, plan)
    _emit({"status": "ok", **result})
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
