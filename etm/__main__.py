"""명령행:  python -m etm demo | serve | seed-demo | examples"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from . import db, seed, server

DEMO_DB = str(db.ROOT / "data" / "etm_demo.db")


def main():
    ap = argparse.ArgumentParser(prog="python -m etm")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo", help="가상 시험 데이터로 실행 (data/etm_demo.db)")
    p.add_argument("--reset", action="store_true", help="시험 DB를 지우고 새로 만든다")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p = sub.add_parser("serve", help="운영 DB로 실행 (기본 data/etm.db, ETM_DB 로 변경)")
    p.add_argument("--db", default=db.DEFAULT_DB)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1")
    p = sub.add_parser("examples", help="입력 예시 JSON 을 examples/ 에 쓴다")
    args = ap.parse_args()

    if args.cmd == "demo":
        if args.reset:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(DEMO_DB + suffix):
                    os.remove(DEMO_DB + suffix)
        conn = db.connect(DEMO_DB)
        seed.seed_demo(conn)
        conn.close()
        server.serve(DEMO_DB, args.host, args.port)
    elif args.cmd == "serve":
        server.serve(args.db, args.host, args.port)
    elif args.cmd == "examples":
        seed.dump_examples(Path(db.ROOT / "examples"))
        print("examples/ 갱신")


if __name__ == "__main__":
    main()
