#!/usr/bin/env python3
import os
from pathlib import Path
os.environ.setdefault("PROXY_SHAKA_HOME", str(Path(__file__).resolve().parent))
from proxy import main
if __name__ == "__main__":
    main()
