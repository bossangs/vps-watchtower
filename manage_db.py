"""No application imports: backup cannot accidentally run migrations or monitoring."""

import argparse
import os
import sqlite3
from pathlib import Path

parser = argparse.ArgumentParser(description="SQLite 一致性备份或完整性检查")
parser.add_argument("action", choices=["backup", "check"])
parser.add_argument("database")
parser.add_argument("destination", nargs="?")
args = parser.parse_args()
source = Path(args.database).resolve()
if not source.is_file():
    parser.error("源数据库不存在")
with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30) as conn:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit("完整性检查失败：" + result)
    if args.action == "backup":
        if not args.destination:
            parser.error("backup 需要目标路径")
        target = Path(args.destination).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Refuse overwriting any existing backup.
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        with sqlite3.connect(target) as dest:
            conn.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise SystemExit("备份校验失败")
        print("备份完成：" + str(target))
    else:
        print("SQLite integrity_check: ok")
