"""Write release/version.json for the update check: version, download URL, size and SHA-256 of the exe."""
import hashlib
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from gitenough import __version__  # noqa: E402

exe = root / "release" / "GitEnough.exe"
data = exe.read_bytes()
info = {
    "version": __version__,
    "url": "https://github.com/KrustyLeBot/GitEnough/raw/main/release/GitEnough.exe",
    "size": len(data),
    "sha256": hashlib.sha256(data).hexdigest(),
}
(root / "release" / "version.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
print(f"version.json: {__version__}, {len(data)} bytes")
