import json
from pathlib import Path

lib = Path(r"C:\Users\kam1k88\ai-remixmate\library")
dirs = [d for d in lib.iterdir() if d.is_dir()]
analyzed = [d for d in dirs if (d / "analysis.json").exists()]
print(f"Analyzed: {len(analyzed)}/{len(dirs)}")
for d in sorted(analyzed)[:10]:
    analysis = json.loads((d / "analysis.json").read_text())
    dur = analysis.get("duration", 0)
    print(f"  {d.name[:50]:50s} {dur:.0f}s ({dur/60:.1f}min)")
