"""Console-free entry point with credential-safe startup diagnostics."""

import ctypes
import sys
import traceback
from pathlib import Path


def main() -> None:
    try:
        from main import main as run

        run()
    except Exception as error:
        log_path = Path.home() / ".simplechart" / "startup-error.log"
        message = "Simple Chart could not start. Try launching simplechart from a terminal."
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Omit exception messages, source text and locals: they may contain credentials.
            frames = traceback.extract_tb(error.__traceback__)
            log_path.write_text(
                "Startup failed. Stack locations (exception details omitted):\n"
                + "".join(f"{frame.filename}:{frame.lineno} in {frame.name}\n" for frame in frames),
                encoding="utf-8",
            )
            message += f"\n\nDiagnostic stack locations: {log_path}"
        except OSError:
            message += "\n\nThe diagnostic file could not be written."
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(None, message, "Simple Chart startup error", 0x10)
        else:
            print(message, file=sys.stderr)
        raise SystemExit(1) from None
