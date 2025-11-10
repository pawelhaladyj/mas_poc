# -*- coding: utf-8 -*-
from prometheus_client import Counter, Histogram, start_http_server

KB_STORE_OK = Counter("kb_store_ok", "Successful STORE ops")
KB_STORE_CONFLICT = Counter("kb_store_conflict", "STORE conflicts")
KB_STORE_FAIL = Counter("kb_store_fail", "STORE failures (other)")
KB_GET_OK = Counter("kb_get_ok", "Successful GET ops")
KB_GET_NOT_FOUND = Counter("kb_get_not_found", "GET not found")
KB_GET_FAIL = Counter("kb_get_fail", "GET failures (other)")


KB_LATENCY = Histogram("kb_op_seconds", "KB operation latency", labelnames=["op"]) # op: store|get


_started = False


def ensure_metrics_server(port:int=9108):
    global _started
    if not _started:
        start_http_server(port)
        _started = True