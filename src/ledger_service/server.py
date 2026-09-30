"""服务启动入口：python -m ledger_service.server [--db path] [--host] [--port]"""
from __future__ import annotations

import argparse

from .http_api import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="非遗活动采购透明台账服务")
    parser.add_argument("--db", default="ledger.sqlite3", help="SQLite 数据库路径")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    server = build_server(args.db, args.host, args.port, verbose=args.verbose)
    print(f"台账服务已启动：http://{args.host}:{args.port} （数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.service.close()
        server.server_close()


if __name__ == "__main__":
    main()
