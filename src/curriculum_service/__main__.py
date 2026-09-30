"""命令行入口：python -m curriculum_service --port 8000 --db data/curriculum.sqlite3"""
from __future__ import annotations

import argparse

from .http_api import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="合作办学课程版本服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default="data/curriculum.sqlite3")
    args = parser.parse_args()

    server = build_server(args.db, args.host, args.port)
    print(f"课程版本服务已启动：http://{args.host}:{args.port}  数据库：{args.db}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.repo.close()


if __name__ == "__main__":
    main()
