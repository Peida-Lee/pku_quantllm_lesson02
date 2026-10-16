"""Recreate official dashboard infrastructure for a thin public source checkout.

Only authored content, theme and reviewed snapshot are copied into a fresh
official preparer output. No protected runtime file is modified.
"""
import shutil
import subprocess
from pathlib import Path

def ensure_runtime(project_root, plugin, node):
    authored = Path(project_root) / "dashboard"
    if (authored / "AGENTS.md").exists(): return authored
    runtime = Path(project_root) / ".dashboard-runtime"
    if not runtime.exists():
        subprocess.run([str(node), str(plugin / "scripts" / "prepare-data-app.mjs"),
                        "--surface", "dashboard", "--output", str(runtime),
                        "--snapshot", str(authored / "src" / "data.json")], check=True, capture_output=True, text=True, timeout=120)
    shutil.copytree(authored / "src" / "content", runtime / "src" / "content", dirs_exist_ok=True)
    shutil.copy2(authored / "src" / "data.json", runtime / "src" / "data.json")
    if (authored / "src" / "theme.css").exists(): shutil.copy2(authored / "src" / "theme.css", runtime / "src" / "theme.css")
    return runtime

if __name__ == "__main__":
    from run_experiments import ROOT, PLUGIN, NODE, build_dashboard
    print(build_dashboard() or "Dashboard build complete")
