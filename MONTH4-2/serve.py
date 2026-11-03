"""
serve.py -- Day 18 (Day 20: monitoring switched on): run the API from a terminal, no notebook needed.

    python serve.py

Reads its settings from environment variables (or a .env file next to it):
    RAG_API_KEYS              key1:rodney,key2:friend       (required, each key >= 16 characters)
    RAG_PACKAGE               rag_pipeline                  (the package freeze_kernel.py wrote)
    RAG_MAX_CONCURRENT        1                             (>1 only after make_concurrency_safe)
    RAG_QUEUE_WAIT_SECONDS    30
    RAG_REQUEST_TIMEOUT_SECONDS 120
    RAG_HOST / RAG_PORT       127.0.0.1 / 8000
    RAG_ENABLE_DOCS           0
    RAG_MONITORING            1                             (Day 20: JSON logs + /metrics; 0 = off)
    RAG_METRICS_KEY           (>= 16 characters, different from every API key; without it /metrics is off)
"""
import importlib
import logging
import os
import sys
from typing import Callable, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from api_layer import ApiConfig, create_app, load_keys_from_env  # noqa: E402

log = logging.getLogger("serve")


def _ping_mongo(obj):
    return obj.admin.command("ping") if hasattr(obj, "admin") else obj.command("ping")


READINESS_SOURCES = [
    ("redis", "redis_client", lambda o: o.ping()),
    ("mongo", "mongo_client", _ping_mongo),
    ("neo4j", "neo4j_driver", lambda o: o.verify_connectivity()),
]


def build_app(env: Optional[Dict[str, str]] = None, pipeline=None):
    env = os.environ if env is None else env
    if pipeline is None:
        package = env.get("RAG_PACKAGE", "rag_pipeline")
        log.info("loading %s.pipeline (connects to your databases and loads models; this can take a while)...", package)
        pipeline = importlib.import_module(package + ".pipeline")
    if not hasattr(pipeline, "run_with_entity_memory"):
        raise SystemExit("the pipeline module has no run_with_entity_memory. Freeze with it as an entry point.")

    max_concurrent = int(env.get("RAG_MAX_CONCURRENT", "1"))
    if max_concurrent > 1:
        tracker = getattr(pipeline, "usage_tracker", None)
        if type(tracker).__name__ != "UsageProxy":
            raise SystemExit(
                f"RAG_MAX_CONCURRENT={max_concurrent} refused: usage_tracker is still one shared global, so overlapping "
                "requests would be billed for each other's tokens. Run make_concurrency_safe.py on the package first, "
                "or set RAG_MAX_CONCURRENT=1.")

    keys = _load_keys(env)
    cfg = ApiConfig(
        api_keys=keys,
        max_concurrent=max_concurrent,
        queue_wait_seconds=float(env.get("RAG_QUEUE_WAIT_SECONDS", "30")),
        request_timeout_seconds=float(env.get("RAG_REQUEST_TIMEOUT_SECONDS", "120")),
        enable_docs=env.get("RAG_ENABLE_DOCS", "0") == "1",
    )

    rate_limit_fn: Optional[Callable] = None
    if hasattr(pipeline, "check_rate_limit"):
        rate_limit_fn = lambda uid: pipeline.check_rate_limit("api:" + uid)   # noqa: E731
    else:
        log.warning("pipeline has no check_rate_limit: the API will NOT rate limit")

    checks = {}
    for name, attr, fn in READINESS_SOURCES:
        obj = getattr(pipeline, attr, None)
        if obj is None:
            log.warning("pipeline has no %s: /readyz will not check %s", attr, name)
            continue
        checks[name] = (lambda o=obj, f=fn: f(o))

    app = create_app(lambda q, s, u: pipeline.run_with_entity_memory(q, s, u), cfg,
                     rate_limit_fn=rate_limit_fn, readiness_checks=checks)
    app.state.monitor = _add_monitoring(app, cfg, pipeline, env, checks)
    return app, cfg


def _monitoring_mode(env) -> str:
    return env.get("RAG_MONITORING", "auto")        # "1" = required, "0" = off, anything else = on if monitoring.py is here


def _add_monitoring(app, cfg, pipeline, env, checks):
    mode = _monitoring_mode(env)
    if mode == "0":
        log.info("monitoring is OFF (RAG_MONITORING=0)")
        return None
    try:
        import monitoring
    except ImportError:
        if mode == "1":
            raise SystemExit("RAG_MONITORING=1 but monitoring.py is not next to serve.py")
        log.warning("monitoring.py not found next to serve.py: running WITHOUT monitoring")
        return None
    try:
        mon, notes = monitoring.setup(app, cfg, pipeline, env, checks)
    except ValueError as e:                          # a bad RAG_METRICS_KEY: refuse to start rather than run half-protected
        raise SystemExit(str(e))
    for n in notes:
        log.info("monitoring: %s", n)
    return mon


def _load_keys(env) -> Dict[str, str]:
    if env is os.environ:
        return load_keys_from_env()
    old = os.environ.get("RAG_API_KEYS")
    os.environ["RAG_API_KEYS"] = env.get("RAG_API_KEYS", "")
    try:
        return load_keys_from_env()
    finally:
        if old is None:
            os.environ.pop("RAG_API_KEYS", None)
        else:
            os.environ["RAG_API_KEYS"] = old


def main():
    dotenv_missing = False
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        dotenv_missing = True
    json_logs = False
    if _monitoring_mode(os.environ) != "0":
        try:
            import monitoring
            monitoring.configure_logging(os.environ)       # one JSON object per line, secrets blanked out
            json_logs = True
        except ImportError:
            pass
    if not json_logs:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if dotenv_missing:
        log.warning("python-dotenv is not installed: only real environment variables will be read")
    import uvicorn
    app, cfg = build_app()
    host, port = os.getenv("RAG_HOST", "127.0.0.1"), int(os.getenv("RAG_PORT", "8000"))
    log.info("serving on http://%s:%s with %d slot(s)", host, port, cfg.max_concurrent)
    # ONE worker process on purpose: the pipeline holds connections, a SQLite checkpoint file and models in memory.
    # With monitoring on, uvicorn must not install its own plain-text log format (log_config=None) and its access
    # line is replaced by ours (access_log=False).
    if json_logs:
        uvicorn.run(app, host=host, port=port, log_level="info", workers=1, log_config=None, access_log=False)
    else:
        uvicorn.run(app, host=host, port=port, log_level="info", workers=1)


if __name__ == "__main__":
    main()
