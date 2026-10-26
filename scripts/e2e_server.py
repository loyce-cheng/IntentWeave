"""Serve browser tests with disposable data, isolated from the user's var/ files."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn


def main():
    with TemporaryDirectory(prefix="worldmind-e2e-") as directory:
        runtime = Path(directory)
        os.environ["WORLDMIND_ENABLE_MODELS"] = "0"
        os.environ["WORLDMIND_LEDGER"] = str(runtime / "ledger.sqlite3")
        os.environ["WORLDMIND_ARCHIVES"] = str(runtime / "archives.sqlite3")
        uvicorn.run("worldmind.api:create_app", factory=True, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
