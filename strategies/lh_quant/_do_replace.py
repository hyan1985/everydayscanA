"""把 searchv1.py 的 run_daily_selection 函数替换为 _new_run.py 里的新版本"""
from pathlib import Path

base = Path(__file__).resolve().parent
src = base / "searchv1.py"
newf = base / "_new_run.py"

lines = src.read_text(encoding="utf-8").splitlines(keepends=True)

# 找到 def run_daily_selection 的行 与 下一个顶层 if __name__ 的行
start = None
end = None
for i, ln in enumerate(lines):
    if ln.startswith("def run_daily_selection("):
        start = i
    elif start is not None and ln.startswith("if __name__"):
        end = i
        break

assert start is not None and end is not None, f"未定位到函数边界 start={start} end={end}"

# 从 _new_run.py 提取新函数体（去掉模块 docstring 第一行，只保留 def 开始的部分，以及末尾的 _build_ths_map 辅助函数）
new_text = newf.read_text(encoding="utf-8")
# 取从第一个 "def run_daily_selection(" 开始的所有内容
idx = new_text.index("def run_daily_selection(")
new_body = new_text[idx:]
if not new_body.endswith("\n"):
    new_body += "\n"
new_body += "\n\n"

result = "".join(lines[:start]) + new_body + "".join(lines[end:])
src.write_text(result, encoding="utf-8")
print(f"替换完成：旧函数 {start+1}-{end} 行 → 新函数 {new_body.count(chr(10))} 行")
