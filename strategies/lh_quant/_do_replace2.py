"""用 new_run_daily_selection.txt（含打分逻辑的完整版）替换 searchv1.py 中
从 def run_daily_selection 到 if __name__ 之间的全部内容。"""
from pathlib import Path

base = Path(__file__).resolve().parent
src = base / "searchv1.py"
newf = base / "new_run_daily_selection.txt"

lines = src.read_text(encoding="utf-8").splitlines(keepends=True)

start = None
end = None
for i, ln in enumerate(lines):
    if start is None and ln.startswith("def run_daily_selection("):
        start = i
    elif start is not None and ln.startswith("if __name__"):
        end = i
        break

assert start is not None and end is not None, f"未定位到边界 start={start} end={end}"

new_body = newf.read_text(encoding="utf-8")
if not new_body.endswith("\n"):
    new_body += "\n"
new_body += "\n\n"

result = "".join(lines[:start]) + new_body + "".join(lines[end:])
src.write_text(result, encoding="utf-8")
print(f"替换完成：旧 {start+1}-{end} 行 → 新函数 {new_body.count(chr(10))} 行")
