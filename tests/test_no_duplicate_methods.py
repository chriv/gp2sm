"""A class that defines the same method twice silently keeps only the last one (ruff doesn't flag it).
This once hid a link-breaking rename_album behind a safe one."""

import ast
import pathlib

import gp2sm


def test_no_class_defines_a_method_twice():
    root = pathlib.Path(gp2sm.__file__).parent
    problems = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ClassDef):
                seen = {}
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name in seen and not item.decorator_list:
                            problems.append(f"{path.relative_to(root)}:{item.lineno} {node.name}.{item.name} "
                                            f"(first at line {seen[item.name]})")
                        seen.setdefault(item.name, item.lineno)
    assert problems == []
