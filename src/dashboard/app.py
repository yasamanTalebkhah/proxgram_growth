"""Dashboard web application for the ProxGram Growth Engine.

FastAPI + Jinja2 (dark glassmorphism UI). Read-only DB access for views;
mutations go through the existing dispatcher/seeder/settings engines.
Listens on DASHBOARD_PORT (default 8080) — never on the user's SOCKS port.
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from src.core.dispatcher import NoDiscussionGroupError, TaskDispatcher
from src.core.seeder import seed_tasks
from src.core.settings import (
    SETTING_DEFS,
    get_setting_int,
    set_setting,
    set_worker_restart_flag,
)
from src.core.spintax_service import spin_preview
from src.core.health import (
    get_worker_heartbeat,
    postgres_ok,
    redis_ok,
    socks_proxy_ok,
)
from src.dashboard.services import (
    accounts_view,
    bulk_import_targets,
    dashboard_kpis,
    delete_target,
    delete_template,
    insert_target,
    insert_template,
    latest_system_logs,
    purge_all_tasks,
    purge_targets,
    set_target_enabled,
    task_detail,
    task_page,
    toggle_template,
    update_template,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("dashboard")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

app = FastAPI(title="ProxGram Growth Dashboard", docs_url=None, redoc_url=None)
dispatcher = TaskDispatcher()

# Phase 2: discovery engine routes (mounted before the /{page} catch-all).
from src.api.routes import router as discovery_router  # noqa: E402

app.include_router(discovery_router)

# Target auto-discovery pipeline: crawler/validator triggers + pool stats.
from src.api.discovery_routes import router as discovery_pipeline_router  # noqa: E402

app.include_router(discovery_pipeline_router)


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    # NOTE: TemplateResponse(request, name) order — newer Starlette requires it.
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/overview")
def api_overview():
    return {
        "kpis": dashboard_kpis(),
        "health": {
            "postgres": postgres_ok(),
            "redis": redis_ok(),
            "worker": get_worker_heartbeat(),
            "socks": socks_proxy_ok(),
        },
    }


@app.get("/api/accounts")
def api_accounts():
    return {"accounts": accounts_view()}


@app.post("/api/accounts/{account_id}/healthcheck")
def api_account_healthcheck(account_id: int):
    from src.dashboard.services import run_account_healthcheck

    return run_account_healthcheck(account_id)


@app.get("/api/channels")
def api_channels():
    from src.dashboard.services import list_targets

    return {"targets": list_targets()}


@app.post("/api/channels")
def api_add_channel(target: str = Form(...), tag: str = Form("")):
    target = target.strip()
    if not target:
        raise HTTPException(400, "Target required")
    try:
        insert_target(target, tag.strip() or None)
    except Exception as exc:
        raise HTTPException(409, f"Could not add target: {exc}")
    return JSONResponse({"ok": True})


@app.post("/api/channels/bulk-import")
def api_bulk_import(payload: dict):
    """Batch Add Targets: paste multi-line targets, dedupe, insert with common tag.

    Body JSON: {"targets": "raw pasted text", "tag": "optional tag"}.
    """
    raw = str((payload or {}).get("targets") or "")
    tag = str((payload or {}).get("tag") or "").strip() or None
    if not raw.strip():
        raise HTTPException(400, "Paste at least one target")
    try:
        return JSONResponse(bulk_import_targets(raw, tag))
    except Exception as exc:
        raise HTTPException(500, f"Bulk import failed: {exc}")


@app.post("/api/channels/{target_id}/toggle")
def api_toggle_channel(target_id: int):
    set_target_enabled(target_id)
    return JSONResponse({"ok": True})


@app.patch("/api/channels/{target_id}/toggle")
def api_toggle_channel_patch(target_id: int):
    set_target_enabled(target_id)
    return JSONResponse({"ok": True})


@app.delete("/api/channels/{target_id}")
def api_delete_channel(target_id: int):
    delete_target(target_id)
    return JSONResponse({"ok": True})


@app.post("/api/channels/purge")
def api_purge_channels():
    """Clear ALL targets (dashboard "Clear All Targets")."""
    cleared = purge_targets()
    return JSONResponse({"ok": True, "cleared": cleared})


@app.get("/api/templates")
def api_templates():
    """Canonical templates listing (aliases to the studio route)."""
    from src.dashboard.services import list_templates

    return {"templates": list_templates()}


@app.get("/api/studio/templates")
def api_studio_templates():
    from src.dashboard.services import list_templates

    return {"templates": list_templates()}


@app.post("/api/templates")
def api_add_template(
    name: str = Form(...),
    template: str = Form(...),
    is_active: bool = Form(False),
):
    if not name.strip() or not template.strip():
        raise HTTPException(400, "Name and template required")
    try:
        insert_template(name.strip(), template, is_active=is_active)
    except Exception as exc:
        raise HTTPException(409, f"Could not add template: {exc}")
    return JSONResponse({"ok": True})


@app.post("/api/studio/templates")
def api_studio_add_template(
    name: str = Form(...),
    template: str = Form(...),
    is_active: bool = Form(False),
):
    return api_add_template(name=name, template=template, is_active=is_active)


@app.put("/api/templates/{template_id}")
async def api_update_template_full(template_id: int, request: Request):
    """Full update: name, template and/or is_active (exclusive activation)."""
    form = dict(await request.form())
    name = str(form.get("name") or "").strip() or None
    template = str(form.get("template") or "").strip() or None
    active_raw = form.get("is_active")
    if name is None and template is None and active_raw is None:
        raise HTTPException(400, "Nothing to update")
    is_active = None if active_raw is None else str(active_raw).lower() in ("1", "true", "on", "yes")
    try:
        updated = update_template(template_id, template=template,
                                  name=name, is_active=is_active)
    except Exception as exc:
        raise HTTPException(409, f"Could not update template: {exc}")
    if not updated:
        raise HTTPException(404, "Template not found")
    return JSONResponse({"ok": True})


@app.post("/api/studio/templates/{template_id}")
def api_studio_update_template(template_id: int, template: str = Form(...)):
    if not update_template(template_id, template=template):
        raise HTTPException(404, "Template not found")
    return JSONResponse({"ok": True})


@app.patch("/api/templates/{template_id}/toggle")
def api_toggle_template(template_id: int):
    toggle_template(template_id)
    return JSONResponse({"ok": True})


@app.post("/api/studio/templates/{template_id}/toggle")
def api_studio_toggle_template(template_id: int):
    toggle_template(template_id)
    return JSONResponse({"ok": True})


@app.delete("/api/templates/{template_id}")
def api_delete_template(template_id: int):
    delete_template(template_id)
    return JSONResponse({"ok": True})


@app.delete("/api/studio/templates/{template_id}")
def api_studio_delete_template(template_id: int):
    delete_template(template_id)
    return JSONResponse({"ok": True})


@app.post("/api/studio/preview")
def api_preview(template: str = Form(...)):
    return {"variants": spin_preview(template, count=3)}


@app.post("/api/studio/test-send")
def api_test_send(target: str = Form(...), template: str = Form("")):
    """Direct one-off test send through the dispatcher's execution path."""
    from src.dashboard.services import run_direct_test_send

    try:
        result = run_direct_test_send(target.strip(), template.strip())
    except Exception as exc:
        raise HTTPException(500, f"Test send failed: {exc}")
    return JSONResponse(result)


@app.get("/api/tasks")
def api_tasks(status: str = "", target: str = "", since: str = "", limit: int = 100):
    return {"tasks": task_page(status=status, target=target, since=since, limit=min(limit, 500))}


@app.get("/api/tasks/{task_id}")
def api_task_detail(task_id: int):
    detail = task_detail(task_id)
    if not detail:
        raise HTTPException(404, "Task not found")
    return detail


@app.post("/api/tasks/{task_id}/cancel")
def api_cancel_task(task_id: int):
    from src.dashboard.services import cancel_task

    if not cancel_task(task_id):
        raise HTTPException(409, "Only PENDING tasks can be cancelled")
    return JSONResponse({"ok": True})


@app.delete("/api/tasks/{task_id}")
def api_purge_task(task_id: int):
    from src.dashboard.services import purge_task

    if not purge_task(task_id):
        raise HTTPException(409, "Only COMPLETED/FAILED/CANCELLED tasks can be purged")
    return JSONResponse({"ok": True})


@app.post("/api/tasks/purge")
def api_purge_tasks():
    """Wipe the entire tasks table + Redis queue keys ("Clear All Tasks")."""
    result = purge_all_tasks()
    return JSONResponse({"ok": True, **result})


@app.post("/api/tasks/seed")
def api_seed_tasks(force: bool = False):
    """Trigger task generation on demand (active targets + active template)."""
    summary = seed_tasks(force=force)
    return {
        "ok": True,
        "seeded": [{"task_id": t, "target": g} for g, t in summary["seeded"]],
        "skipped": summary["skipped"],
    }


@app.post("/api/actions/seed")
def api_action_seed(force: bool = False):
    summary = seed_tasks(force=force)
    return {
        "ok": True,
        "seeded": [{"task_id": t, "target": g} for g, t in summary["seeded"]],
        "skipped": summary["skipped"],
    }


@app.post("/api/actions/sweep")
def api_action_sweep():
    counts = dispatcher.sweep_stale_tasks()
    return {"ok": True, **counts}


@app.post("/api/actions/requeue")
def api_action_requeue():
    return {"ok": True, "requeued": dispatcher.requeue_failed_tasks()}


@app.post("/api/tasks/{task_id}/retry")
def api_retry_task(task_id: int):
    """Manually requeue a FAILED task immediately (no backoff wait)."""
    if not dispatcher.retry_task(task_id):
        raise HTTPException(
            409, "Task not retryable (must be FAILED with retries remaining)"
        )
    return JSONResponse({"ok": True})


@app.get("/api/settings")
def api_get_settings():
    from src.core.settings import get_setting

    return {
        "defs": SETTING_DEFS,
        "values": {d["key"]: get_setting(d["key"]) for d in SETTING_DEFS},
    }


@app.post("/api/settings")
async def api_save_settings(request: Request):
    """Validate every provided value first, then persist — all-or-nothing."""
    pending, errors = [], []
    form = dict(await request.form())
    for definition in SETTING_DEFS:
        key = definition["key"]
        raw = form.get(key, "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            errors.append(f"{key}: must be an integer")
            continue
        if not definition["min"] <= value <= definition["max"]:
            errors.append(f"{key}: must be {definition['min']}–{definition['max']}")
            continue
        pending.append((key, value))
    if errors:
        raise HTTPException(400, "; ".join(errors))
    saved = []
    for key, value in pending:
        set_setting(key, str(value))
        saved.append(f"{key}={value}")
    return {"ok": True, "saved": saved}


@app.post("/api/worker/reload")
def api_worker_reload():
    set_worker_restart_flag(True)
    return {"ok": True, "detail": "Worker will reload within ~60s (next settings poll)."}


@app.get("/api/logs")
def api_logs(limit: int = 200):
    return {"entries": latest_system_logs(limit=min(limit, 500))}


@app.get("/health")
def health():
    return {
        "dashboard": "ok",
        "postgres": postgres_ok(),
        "redis": redis_ok(),
        "worker": get_worker_heartbeat(),
    }


# Page routes LAST so the /{page} catch-all never shadows API or /health.
@app.get("/{page}", response_class=HTMLResponse)
def page(request: Request, page: str):
    if page not in {"accounts", "channels", "templates", "studio", "tasks", "settings", "logs"}:
        raise HTTPException(404, "Not found")
    return templates.TemplateResponse(request, "index.html")


def main() -> None:
    import uvicorn

    host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("DASHBOARD_PORT", "8080"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
