"""刷新《后端手册》里从源码生成的内容：第 7 节每个文件的函数表（行号、名字、做什么、谁在用），
和第 6 节接口索引里每个接口的行号。

为什么要有这个脚本：手册里的函数表是从源码生成的，不是手写的。代码一改，行号就会变，也可能多出或少掉函数；
改完代码运行一下它，表就和代码重新对上了。表里的“做什么”取自每个函数文档字符串的第一行，“谁在用”取自
文档字符串里 “谁在用：” 那一段，所以改了函数的用途或调用方，记得同时改它的文档字符串。

用法（在项目根目录运行）：
    .venv\\Scripts\\python.exe docs\\refresh_tables.py            刷新 docs/后端手册.md 里的表
    .venv\\Scripts\\python.exe docs\\refresh_tables.py --check    只检查，不改文件；表和代码对不上时退出码是 1

它怎么找到该改的地方：
  - 函数表：手册里每个文件的小节有一行 “<!-- TABLE: 文件路径 -->”，表就放在这一行和 “<!-- /TABLE -->” 之间。
    脚本只替换这两行之间的内容。新加了源文件的话，先在手册里给它写一个小节、加上这一行标记，再运行脚本；
    脚本会提醒哪些文件还没有标记。
  - 接口索引：第 6 节的表里，每一行以 “| `GET /api/…` |” 这样的“方法 路径”开头，第三格是处理函数所在的行号。
    脚本只改这个数字。新加了接口的话，自己在第 6 节加一行（行号随便填一个数），再运行脚本。
手册里其他的文字一个字都不动。

它只读源文件（用 Python 自带的 ast 模块分析，不导入、不运行后端代码），只写 docs/后端手册.md 这一个文件。
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

# 项目根目录：这个脚本在 docs/ 下，往上一层
ROOT = Path(__file__).resolve().parent.parent
MANUAL = ROOT / "docs" / "后端手册.md"

# 文档字符串里 “谁在用：” 这一段的开头（几种写法都认）
_USERS = re.compile(r"^\s*(谁在用|谁调用它|谁调用|调用方)\s*[：:]\s*(.*)$")
# 文档字符串里另一段的开头，形如 “参数：”“返回：”“副作用：没有。”；遇到它，“谁在用” 这一段就结束了
_NEXT_SECTION = re.compile(r"^\s{0,4}[^\s：:（）()，。、]{1,12}[：:](\s|$)")
# 手册里的标记行
_OPEN = re.compile(r"^<!-- TABLE: (?P<path>\S+) -->\s*$")
_CLOSE = "<!-- /TABLE -->"
# 路由装饰器的方法名：@router.get(...)、@app.post(...) 这类
_HTTP_METHODS = ("get", "post", "put", "patch", "delete")
# 第 6 节接口索引里的一行：| `GET /api/…` | 处理函数 | 行号 | …。分成三段：行号之前、行号、行号之后
_ROUTE_ROW = re.compile(
    r"^(?P<head>\| `(?P<route>(?:GET|POST|PUT|PATCH|DELETE) /[^`]*)` \|[^|]*\| )(?P<line>\d+)(?P<tail> \|.*)$"
)


def source_files() -> list[Path]:
    """要列进手册的源文件：根目录的 main.py 和 syntherisk/ 下的全部 .py（不含缓存目录）。"""
    found = [ROOT / "main.py"] + sorted((ROOT / "syntherisk").rglob("*.py"))
    return [path for path in found if "__pycache__" not in path.parts]


def doc_parts(node: ast.AST) -> tuple[str, str]:
    """从一个模块、类或函数的文档字符串里取出两样东西：第一行，以及 “谁在用” 那一段（没有就是空字符串）。"""
    lines = (ast.get_docstring(node) or "").strip().splitlines()
    first = lines[0].strip() if lines else ""
    users: list[str] = []
    collecting = False
    for line in lines[1:]:
        match = _USERS.match(line)
        if match:
            collecting = True
            if match.group(2).strip():
                users.append(match.group(2).strip())
        elif collecting:
            # 空行，或者另一段的开头：这一段到此为止
            if not line.strip() or _NEXT_SECTION.match(line):
                break
            users.append(line.strip())
    return first, " ".join(users)


def comment_above(source_lines: list[str], lineno: int) -> str:
    """某一行正上方连着的 # 注释（常量和一行写完的函数没有文档字符串，说明写在这里）。"""
    found: list[str] = []
    index = lineno - 2  # lineno 从 1 开始；它的上一行在列表里的下标是 lineno - 2
    while index >= 0 and source_lines[index].strip().startswith("#"):
        found.append(source_lines[index].strip().lstrip("#").strip())
        index -= 1
    return " ".join(reversed(found))


def route_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """接口函数对应的 “方法 路径”，比如 “POST /api/analyses”；不是接口函数时返回空字符串。

    api/ 下的接口登记在各文件的 router 上，挂载时统一加了 /api 前缀（syntherisk/main.py 的 create_app）；
    直接登记在 app 上的（/、/health）没有前缀。
    """
    for decorator in node.decorator_list:
        if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in _HTTP_METHODS and decorator.args
                and isinstance(decorator.args[0], ast.Constant)):
            prefix = "/api" if ast.unparse(decorator.func.value) == "router" else ""
            return f"{decorator.func.attr.upper()} {prefix}{decorator.args[0].value}"
    return ""


def hidden_from_docs(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """这个接口是不是特意不放进接口文档的（装饰器里写了 include_in_schema=False，比如跳转到 /docs 的根地址）。"""
    return any(
        keyword.arg == "include_in_schema" and isinstance(keyword.value, ast.Constant) and keyword.value.value is False
        for decorator in node.decorator_list if isinstance(decorator, ast.Call)
        for keyword in decorator.keywords
    )


def collect(body: list[ast.stmt], owner: str, inside_function: bool, source_lines: list[str], rows: list[dict]) -> None:
    """把一段代码里的常量、类、函数（连同类里的方法、函数里的内部函数）按出现的顺序收进 rows。

    参数：
        body             模块、类或函数的语句列表。
        owner            外层的名字：模块最外层是空字符串，类里是 “类名”，函数里是 “函数名()”。
        inside_function  这段代码是不是在某个函数里面（里面的函数算 “内部函数”）。
        source_lines     源文件的各行，取注释用。
        rows             结果列表，每项是一个字典：kind、name、line、doc、users、route。
    """
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            first, users = doc_parts(node)
            # 带装饰器的函数，注释在装饰器的上面
            top = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list])
            route = route_of(node)
            rows.append({
                "kind": "nested" if inside_function else ("method" if owner else "function"),
                "name": f"{owner}.{node.name}" if owner else node.name,
                "line": node.lineno,
                "doc": first or comment_above(source_lines, top),
                "users": users or ("前端（HTTP 接口）" if route else ""),
                "route": route,
                "hidden": hidden_from_docs(node),
            })
            collect(node.body, f"{owner}.{node.name}()" if owner else f"{node.name}()", True, source_lines, rows)
        elif isinstance(node, ast.ClassDef):
            first, users = doc_parts(node)
            name = f"{owner}.{node.name}" if owner else node.name
            rows.append({"kind": "class", "name": name, "line": node.lineno,
                         "doc": first or comment_above(source_lines, node.lineno), "users": users, "route": ""})
            collect(node.body, name, False, source_lines, rows)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and not owner:
            # 只收模块最外层的赋值（常量、router、logger 这些）；类里的字段在类的文档字符串里说明
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    rows.append({"kind": "constant", "name": target.id, "line": node.lineno,
                                 "doc": comment_above(source_lines, node.lineno), "users": "", "route": ""})


def cell(text: str, limit: int) -> str:
    """整理成表格里的一格：竖线要转义，太长的截断。"""
    text = text.replace("|", "\\|").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def rows_of(path: Path) -> list[dict]:
    """一个源文件里的全部常量、类、函数，按出现的顺序（每项的键见 collect）。"""
    source = path.read_text(encoding="utf-8")
    rows: list[dict] = []
    collect(ast.parse(source).body, "", False, source.splitlines(), rows)
    return rows


def table_for(path: Path) -> list[str]:
    """一个源文件的函数表（Markdown 的各行）。"""
    rows = rows_of(path)
    if not rows:
        return ["这个文件里没有常量、类和函数，只有文件开头的说明。"]
    labels = {"class": "类 ", "constant": "常量 ", "nested": "内部函数 ", "method": "", "function": ""}
    lines = ["| 行 | 名字 | 做什么 | 谁在用 |", "|---|---|---|---|"]
    for row in rows:
        name = f"{labels[row['kind']]}`{row['name']}`" + (f"<br>**{row['route']}**" if row["route"] else "")
        lines.append(f"| {row['line']} | {name} | {cell(row['doc'], 160)} | {cell(row['users'], 280)} |")
    return lines


def refresh(text: str) -> tuple[str, list[str]]:
    """把手册正文里每一对标记之间的内容换成现在的函数表。返回 (新的正文, 要提醒的事)。"""
    known = {path.relative_to(ROOT).as_posix(): path for path in source_files()}
    seen: list[str] = []
    notes: list[str] = []
    out: list[str] = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        match = _OPEN.match(lines[index])
        if not match:
            out.append(lines[index])
            index += 1
            continue
        name = match.group("path")
        out.append(lines[index])
        index += 1
        # 跳过旧表：从标记的下一行起，到结束标记为止。没有结束标记（第一次运行）就什么都不跳
        end = index
        while end < len(lines) and lines[end].strip() != _CLOSE and not _OPEN.match(lines[end]):
            end += 1
        if end < len(lines) and lines[end].strip() == _CLOSE:
            index = end + 1
        if name in known:
            out.extend(["", *table_for(known[name]), "", _CLOSE])
            seen.append(name)
        else:
            out.extend(["", f"（找不到文件 {name}：它可能被改名或删掉了）", "", _CLOSE])
            notes.append(f"手册里有 {name} 的表，但找不到这个文件")
    notes += [f"{name} 在手册里出现了 {seen.count(name)} 次" for name in sorted(set(seen)) if seen.count(name) > 1]
    notes += [f"手册里还没有 {name} 的小节（要加一行 <!-- TABLE: {name} -->）" for name in known if name not in seen]

    # 第二遍：第 6 节接口索引里的行号。route_lines 是 “方法 路径” → 处理函数 def 那一行的行号
    handlers = [row for path in known.values() for row in rows_of(path) if row["route"]]
    route_lines = {row["route"]: row["line"] for row in handlers}
    indexed: set[str] = set()
    for position, line in enumerate(out):
        match = _ROUTE_ROW.match(line)
        if not match:
            continue
        route = match.group("route")
        indexed.add(route)
        if route in route_lines:
            out[position] = f"{match.group('head')}{route_lines[route]}{match.group('tail')}"
        else:
            notes.append(f"手册第 6 节里有 {route}，但代码里找不到这个接口")
    if indexed:  # 手册里有接口索引时，才提醒哪些接口还没有列进去（特意不进接口文档的不提醒）
        notes += [f"手册第 6 节里还没有 {row['route']} 这个接口"
                  for row in handlers if row["route"] not in indexed and not row["hidden"]]
    return "\n".join(out), notes


def main() -> int:
    """命令行入口。返回退出码：0 正常；1 是 --check 时发现表和代码对不上，或者有要提醒的事。"""
    check_only = "--check" in sys.argv[1:]
    old = MANUAL.read_text(encoding="utf-8")
    new, notes = refresh(old)
    for note in notes:
        print("注意：", note)
    if new == old:
        print("函数表和代码一致，不用改。")
        return 1 if notes else 0
    if check_only:
        print("函数表和代码对不上了：运行 docs\\refresh_tables.py（不带 --check）刷新。")
        return 1
    MANUAL.write_text(new, encoding="utf-8", newline="\n")
    print(f"已刷新 {MANUAL.relative_to(ROOT).as_posix()} 里的函数表。")
    return 1 if notes else 0


if __name__ == "__main__":
    raise SystemExit(main())
