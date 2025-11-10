#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kbctl — narzędzie operacyjne do przeglądania KB po DSN (bez XMPP).
Przykłady:
./kbctl.py --dsn postgresql://user:pass@127.0.0.1:5432/mas_kb get \
--key session:sess-123:chat:timeline:main
./kbctl.py --dsn ... dump --session sess-123
"""
import argparse, sys, json
import psycopg2


SQL_LAST = """
SELECT content_type, value, version, etag::text, created_at
FROM kb_items WHERE key=%s AND deleted=false
ORDER BY version DESC LIMIT 1;
"""
SQL_BY_VER = """
SELECT content_type, value, version, etag::text, created_at
FROM kb_items WHERE key=%s AND version=%s AND deleted=false LIMIT 1;
"""
SQL_DUMP_SESSION = """
SELECT key, version, etag::text, created_at
FROM kb_items WHERE session_id=%s AND deleted=false
ORDER BY key, version;
"""


def connect(dsn):
    return psycopg2.connect(dsn)


def cmd_get(conn, key, version=None):
    cur = conn.cursor()
    if version is None:
        cur.execute(SQL_LAST, (key,))
    else:
        cur.execute(SQL_BY_VER, (key, int(version)))
    row = cur.fetchone()
    if not row:
        print("NOT_FOUND", file=sys.stderr); sys.exit(2)
    ctype, val, ver, etag, ts = row
    print(json.dumps({
        "key": key, "version": ver, "etag": etag,
        "content_type": ctype, "stored_at": ts.isoformat(),
        "value": val
    }, ensure_ascii=False, indent=2))


def cmd_dump(conn, session_id):
    cur = conn.cursor()
    cur.execute(SQL_DUMP_SESSION, (session_id,))
    rows = cur.fetchall()
    for r in rows:
        print(f"{r[0]} v{r[1]} etag={r[2]} @ {r[3].isoformat()}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    sub = ap.add_subparsers(dest="cmd", required=True)

    ap_get = sub.add_parser("get")
    ap_get.add_argument("--key", required=True)
    ap_get.add_argument("--version", type=int)

    ap_dump = sub.add_parser("dump")
    ap_dump.add_argument("--session", required=True)

    args = ap.parse_args()
    conn = connect(args.dsn)
    try:
        if args.cmd == "get":
            cmd_get(conn, args.key, args.version)
        elif args.cmd == "dump":
            cmd_dump(conn, args.session)
    finally:
        conn.close()


if __name__ == "__main__":
    main()