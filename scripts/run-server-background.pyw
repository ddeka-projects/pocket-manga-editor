"""Console-free scheduled entry point; the task tracks this server process."""

import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
os.chdir(root)
sys.path.insert(0, str(root))

from pocket_manga_editor.logging_setup import configure_logging, redirect_standard_streams

configure_logging(console=False)
redirect_standard_streams()
try:
    from pocket_manga_editor.app import main
    raise SystemExit(main())
except SystemExit:
    raise
except BaseException:
    import logging
    logging.getLogger(__name__).exception("Background server failed")
    raise SystemExit(1)
finally:
    sys.stdout.flush()
    sys.stderr.flush()
