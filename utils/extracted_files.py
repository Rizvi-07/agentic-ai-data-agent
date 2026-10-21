"""Files created by extraction tasks, separate from the shared source datasets."""
import csv
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
FILE_TYPES = {".csv", ".json", ".parquet"}


class ExtractedFiles:
    def __init__(self, folder=None):
        self.folder = Path(folder or ROOT / "data" / "extract").resolve()

    def resolve(self, relative):
        path = (self.folder / relative).resolve()
        if (not path.is_relative_to(self.folder) or path.suffix.lower() not in FILE_TYPES
                or not path.is_file()):
            raise FileNotFoundError("Extracted file not found.")
        return path

    def info(self, path):
        stat = path.stat()
        return {"path": path.relative_to(self.folder).as_posix(), "name": path.name,
                "size": stat.st_size, "modified": stat.st_mtime_ns}

    def delete(self, relative):
        # resolve() checks containment, file type and existence before deletion.
        path = self.resolve(relative)
        file = self.info(path)
        path.unlink()
        return file

    def list(self):
        files = []
        if self.folder.exists():
            for path in self.folder.rglob("*"):
                if path.suffix.lower() in FILE_TYPES and path.is_file() and path.resolve().is_relative_to(self.folder):
                    try:
                        files.append(self.info(path))
                    except FileNotFoundError:
                        # Another request may delete an extract while listing.
                        continue
        return sorted(files, key=lambda item: item["modified"], reverse=True)[:100]

    def save_csv(self, result, prefix):
        self.folder.mkdir(parents=True, exist_ok=True)
        prefix = re.sub(r"[^a-zA-Z0-9_-]", "_", prefix).strip("_")[:50] or "query"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = self.folder / f"{prefix}_{stamp}_{uuid4().hex[:8]}.csv"
        # Atomic publication means the download list never sees a partial CSV.
        temporary = path.with_suffix(".tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(result["columns"])
                writer.writerows(result["rows"])
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return self.info(path)
