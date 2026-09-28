import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QGuiApplication  # noqa: E402

from gitenough.style import app_icon  # noqa: E402

app = QGuiApplication(sys.argv)
out = Path(__file__).resolve().parents[1] / "build" / "icon.ico"
out.parent.mkdir(exist_ok=True)
if not app_icon(256).save(str(out), "ICO"):
    sys.exit("ICO export failed")
print(out)
