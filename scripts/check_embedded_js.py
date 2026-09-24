from pathlib import Path
import re
import subprocess
import tempfile

source = Path("app.py").read_text(encoding="utf-8")
blocks = re.findall(r"<script>(.*?)</script>", source, flags=re.S)
if not blocks:
    raise SystemExit("No embedded JavaScript blocks found")

for i, block in enumerate(blocks):
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(block)
        path = f.name
    result = subprocess.run(
        ["node", "--check", path],
        capture_output=True,
        text=True,
    )
    Path(path).unlink(missing_ok=True)
    if result.returncode != 0:
        raise SystemExit(
            f"Embedded JavaScript block {i} failed syntax check:\n{result.stderr}"
        )

print(f"Embedded JavaScript syntax OK: {len(blocks)} blocks")
