import json
import hmac
import threading
import time
from datetime import datetime
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings, validate_settings
from .db import init_db, db_healthcheck
from .utils import find_available_port
from .worker import start_worker_once, worker_status
from . import services, warmup

settings = get_settings()
app = FastAPI(title="SendGrid Web Admin Scheduler", version="3.2.0")
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.session_cookie_secure,
    max_age=settings.session_max_age_seconds,
)
templates = Jinja2Templates(directory="app/templates")

RECIPIENT_EXTENSIONS = {".txt", ".csv"}
TEMPLATE_EXTENSIONS = {".html", ".htm"}


@app.on_event("startup")
def startup():
    validate_settings(settings)
    init_db()
    start_worker_once()


def require_login(request: Request):
    if not request.session.get("logged_in"):
        raise HTTPException(status_code=401, detail="Not logged in")
    cached = request.session.get("user") or {}
    user_id = cached.get("id")
    current = services.get_active_user_session(user_id) if user_id else None
    if not current:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    if current != cached:
        request.session["user"] = current
    return current


def require_admin(request: Request):
    user = require_login(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin permission required")
    return user


def _file_extension(filename):
    name = (filename or "").lower().strip()
    if "." not in name:
        return ""
    return "." + name.rsplit(".", 1)[1]




def _script_json(value):
    """Keep JSON values from ending an inline script element."""
    return (json.dumps(str(value), ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def _alert_redirect(message, target="/#tasks"):
    return HTMLResponse("""
<!doctype html><meta charset="utf-8"><script>
alert(%s);
location.href = %s;
</script>
""" % (_script_json(message), _script_json(target)))


async def _read_limited_upload(file: UploadFile, allowed_extensions, max_bytes, label):
    filename = file.filename or ""
    ext = _file_extension(filename)
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail="{} file type is not allowed: {}".format(label, filename or "(unnamed)")
        )

    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail="{} file is too large. Max allowed is {} bytes.".format(label, max_bytes)
        )
    if not content:
        raise HTTPException(
            status_code=400,
            detail="{} file is empty: {}".format(label, filename or "(unnamed)")
        )
    return content



_login_lock = threading.Lock()
_login_failures = {}


def _login_client_key(request):
    return (request.client.host if request.client else "unknown")


def _login_is_blocked(key):
    cutoff = time.time() - settings.login_window_seconds
    with _login_lock:
        recent = [stamp for stamp in _login_failures.get(key, []) if stamp >= cutoff]
        _login_failures[key] = recent
        return len(recent) >= settings.login_max_attempts


def _record_login_failure(key):
    with _login_lock:
        if len(_login_failures) > 10000:
            # The limiter is intentionally in-memory; keep it bounded under a
            # distributed credential-stuffing attempt.
            _login_failures.clear()
        _login_failures.setdefault(key, []).append(time.time())


def _clear_login_failures(key):
    with _login_lock:
        _login_failures.pop(key, None)


@app.middleware("http")
async def same_origin_post_guard(request: Request, call_next):
    """Reject browser cross-origin state-changing requests.

    SameSite cookies remain enabled as a second layer. Requests without Origin
    or Referer are retained for compatibility with non-browser clients.
    """
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path != "/api/sendgrid/events":
        source = request.headers.get("origin") or request.headers.get("referer")
        if source:
            parsed = urlsplit(source)
            request_host = (request.headers.get("host") or "").lower()
            if parsed.netloc.lower() != request_host:
                return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
    return await call_next(request)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not request.session.get("logged_in"):
        return templates.TemplateResponse("login.html", {"request": request, "error": None})
    try:
        current_user = require_login(request)
    except HTTPException:
        return templates.TemplateResponse("login.html", {"request": request, "error": "会话已失效，请重新登录。"})
    data = services.get_dashboard_data()
    data.update(warmup.dashboard_data())
    data["request"] = request
    data["current_user"] = current_user
    return templates.TemplateResponse("admin.html", data)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    key = _login_client_key(request)
    if _login_is_blocked(key):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "登录失败次数过多，请稍后再试。"},
            status_code=429,
        )
    user = services.authenticate_user(username, password)
    if user:
        _clear_login_failures(key)
        request.session.clear()
        request.session["logged_in"] = True
        request.session["user"] = user
        return RedirectResponse("/", status_code=303)
    _record_login_failure(key)
    return templates.TemplateResponse("login.html", {"request": request, "error": "账号或密码错误，或账号已停用"})


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)



@app.post("/users/create")
def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    role: str = Form("member"),
    status: str = Form("active"),
):
    require_admin(request)
    try:
        services.create_user(username, password, display_name, role, status)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#users")
    return RedirectResponse("/#users", status_code=303)


@app.post("/users/{user_id}/update")
def update_user(
    request: Request,
    user_id: int,
    display_name: str = Form(""),
    role: str = Form("member"),
    status: str = Form("active"),
    password: str = Form(""),
):
    current = require_admin(request)
    try:
        services.update_user(user_id, display_name, role, status, password)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#users")
    if int(current.get("id") or 0) == user_id:
        # Refresh current session if admin edited himself/herself.
        current["display_name"] = display_name or current.get("username")
        current["role"] = role
        request.session["user"] = current
    return RedirectResponse("/#users", status_code=303)


@app.post("/tags/create")
def create_tag(request: Request, name: str = Form(...), service_type: str = Form(...), remark: str = Form("")):
    require_login(request)
    services.create_tag(name, service_type, remark)
    return RedirectResponse("/#settings", status_code=303)


@app.post("/proxies/create")
def create_proxy(
    request: Request,
    name: str = Form(...),
    proxy_url: str = Form(""),
    test_now: str = Form(""),
):
    require_login(request)
    proxy_id = services.create_proxy(name, proxy_url)
    if test_now:
        result = services.test_proxy(proxy_id)
        return _alert_redirect(result.get("message") or "代理测试完成", "/#settings")
    return RedirectResponse("/#settings", status_code=303)


@app.post("/proxies/{proxy_id}/test")
def test_proxy(request: Request, proxy_id: int):
    require_login(request)
    try:
        result = services.test_proxy(proxy_id)
        return _alert_redirect(result.get("message") or "代理测试完成", "/#settings")
    except Exception as exc:
        return _alert_redirect("代理测试失败：{}".format(exc), "/#settings")


@app.post("/channels/create")
def create_channel(
    request: Request,
    tag_id: int = Form(...),
    name: str = Form(...),
    api_key: str = Form(...),
    from_email: str = Form(...),
    from_name: str = Form(""),
    proxy_id: str = Form(""),
    daily_limit: int = Form(500),
):
    require_login(request)
    services.create_channel(tag_id, name, api_key, from_email, from_name, int(proxy_id) if proxy_id else None, daily_limit)
    return RedirectResponse("/#settings", status_code=303)


@app.post("/channels/{channel_id}/update")
def update_channel(
    request: Request,
    channel_id: int,
    tag_id: int = Form(...),
    name: str = Form(...),
    api_key: str = Form(""),
    from_email: str = Form(...),
    from_name: str = Form(""),
    proxy_id: str = Form(""),
    daily_limit: int = Form(500),
    status: str = Form("active"),
):
    require_login(request)
    services.update_channel(channel_id, tag_id, name, api_key, from_email, from_name, int(proxy_id) if proxy_id else None, daily_limit, status)
    return RedirectResponse("/#settings", status_code=303)


@app.post("/recipients/upload")
async def recipients_upload(
    request: Request,
    tag_id: int = Form(...),
    pool_type: str = Form(...),
    name: str = Form(""),
    files: list[UploadFile] = File(...),
):
    require_login(request)
    s = get_settings()
    if not files:
        return _alert_redirect("请至少选择一个 TXT/CSV 收件人文件。", "/#recipients")
    if len(files) > s.max_recipient_files_per_upload:
        raise HTTPException(
            status_code=413,
            detail="Too many recipient files. Max allowed is {}.".format(s.max_recipient_files_per_upload),
        )

    summaries = []
    total_parsed = 0
    total_imported = 0
    total_duplicates = 0
    total_invalid = 0
    failed = []

    for file in files:
        filename = file.filename or "未命名文件"
        try:
            content = await _read_limited_upload(file, RECIPIENT_EXTENSIONS, s.max_recipient_upload_bytes, "recipient pool")
            source = name.strip() if name and name.strip() else filename
            result = services.import_recipient_pool(tag_id, pool_type, source, content)
            total_parsed += int(result.get("parsed") or 0)
            total_imported += int(result.get("imported") or 0)
            total_duplicates += int(result.get("duplicates") or 0)
            total_invalid += int(result.get("invalid") or 0)
            summaries.append(
                "{}：解析 {}，新增 {}，重复 {}，无效 {}".format(
                    filename,
                    result.get("parsed") or 0,
                    result.get("imported") or 0,
                    result.get("duplicates") or 0,
                    result.get("invalid") or 0,
                )
            )
        except (HTTPException, ValueError) as exc:
            detail = getattr(exc, "detail", str(exc))
            failed.append("{}：{}".format(filename, detail))

    message = "多文件导入完成。\n总解析：{}\n新增入库：{}\n重复忽略：{}\n无效行：{}".format(
        total_parsed, total_imported, total_duplicates, total_invalid
    )
    if summaries:
        message += "\n\n文件明细：\n" + "\n".join(summaries[:20])
        if len(summaries) > 20:
            message += "\n... 其余 {} 个文件已导入".format(len(summaries) - 20)
    if failed:
        message += "\n\n失败文件：\n" + "\n".join(failed[:20])
        if len(failed) > 20:
            message += "\n... 其余 {} 个文件失败".format(len(failed) - 20)
    return _alert_redirect(message, "/#recipients")


@app.post("/recipients/{recipient_id}/update")
def recipients_update(
    request: Request,
    recipient_id: int,
    tag_id: int = Form(...),
    pool_type: str = Form(...),
    email: str = Form(...),
    name: str = Form(""),
    source_name: str = Form(""),
):
    require_login(request)
    try:
        services.update_recipient_pool_entry(
            recipient_id, tag_id, pool_type, email, name, source_name
        )
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#recipients")
    return _alert_redirect("收件人已更新。", "/#recipients")


@app.post("/recipients/{recipient_id}/delete")
def recipients_delete(request: Request, recipient_id: int):
    require_login(request)
    try:
        result = services.delete_recipient_pool_entry(recipient_id)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#recipients")
    return _alert_redirect("已删除收件人：{}".format(result.get("email") or recipient_id), "/#recipients")


@app.post("/recipients/pool/delete-available")
def recipients_delete_available(
    request: Request,
    tag_id: int = Form(...),
    pool_type: str = Form(...),
):
    require_login(request)
    try:
        result = services.delete_available_recipient_pool(tag_id, pool_type)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#recipients")
    return _alert_redirect(
        "已删除当前库中 {} 条未使用收件人。".format(result.get("deleted") or 0),
        "/#recipients",
    )


@app.post("/templates/upload")
async def templates_upload(
    request: Request,
    tag_id: int = Form(...),
    name: str = Form(...),
    files: list[UploadFile] = File(...),
):
    require_login(request)
    s = get_settings()
    if not files:
        raise HTTPException(status_code=400, detail="At least one template file is required.")
    if len(files) > s.max_template_files_per_upload:
        raise HTTPException(
            status_code=413,
            detail="Too many template files. Max allowed is {}.".format(s.max_template_files_per_upload)
        )

    validated_files = []
    for f in files:
        content = await _read_limited_upload(
            f, TEMPLATE_EXTENSIONS, s.max_template_upload_bytes, "template"
        )
        validated_files.append((f.filename, content))

    group_id = services.create_template_group(tag_id, name)
    for filename, content in validated_files:
        services.save_template_file(group_id, filename, content)
    return RedirectResponse("/#templates", status_code=303)


@app.post("/templates/{group_id}/update")
def template_group_update(
    request: Request,
    group_id: int,
    tag_id: int = Form(...),
    name: str = Form(...),
    status: str = Form("active"),
):
    require_login(request)
    services.update_template_group(group_id, tag_id, name, status)
    return RedirectResponse("/#templates", status_code=303)


@app.post("/templates/{group_id}/delete")
def template_group_delete(request: Request, group_id: int):
    require_login(request)
    try:
        services.delete_template_group(group_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse("/#templates", status_code=303)


@app.post("/templates/files/{file_id}/update")
def template_file_update(
    request: Request,
    file_id: int,
    html_content: str = Form(""),
):
    require_login(request)
    try:
        services.update_template_file_content(file_id, html_content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse("/#templates", status_code=303)


@app.post("/warmup/lists/upload")
async def warmup_list_upload(
    request: Request,
    tag_id: int = Form(...),
    name: str = Form(...),
    consent_source: str = Form(...),
    consented_at: str = Form(...),
    files: list[UploadFile] = File(...),
):
    require_login(request)
    s = get_settings()
    try:
        if not files:
            raise ValueError("请至少选择一个 TXT/CSV 收件人文件。")
        if len(files) > s.max_recipient_files_per_upload:
            raise ValueError("收件人文件超过数量限制（最多 {} 个）。".format(s.max_recipient_files_per_upload))
        contents = []
        for file in files:
            contents.append(await _read_limited_upload(
                file, RECIPIENT_EXTENSIONS, s.max_recipient_upload_bytes, "warmup list"
            ))
        warmup.import_named_list(tag_id, name, consent_source, consented_at, b"\n".join(contents))
    except (HTTPException, ValueError) as exc:
        return _alert_redirect(getattr(exc, "detail", str(exc)), "/#warmup")
    return RedirectResponse("/#warmup", status_code=303)


@app.post("/warmup/create")
def warmup_create(
    request: Request,
    tag_id: int = Form(...),
    channel_id: int = Form(...),
    name: str = Form(...),
    subject_template: str = Form(...),
    template_group_id: int = Form(...),
    day_counts: list[str] = Form(...),
    source_pool_types: list[str] = Form([]),
    source_list_ids: list[int] = Form([]),
    interval_mode: str = Form(...),
    interval_seconds: str = Form(""),
    consent_confirmed: str = Form(""),
):
    require_login(request)
    try:
        warmup.create_task(
            tag_id, channel_id, name, subject_template, template_group_id,
            day_counts, interval_mode, interval_seconds,
            source_pool_types, source_list_ids, consent_confirmed,
        )
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#warmup")
    return RedirectResponse("/#warmup", status_code=303)


@app.post("/warmup/lists/{list_id}/disable")
def warmup_list_disable(request: Request, list_id: int):
    require_login(request)
    try:
        warmup.disable_named_list(list_id)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#warmup")
    return RedirectResponse("/#warmup", status_code=303)


@app.post("/warmup/{task_id}/start")
def warmup_start(request: Request, task_id: int):
    require_login(request)
    try:
        warmup.start_task(task_id)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#warmup")
    return RedirectResponse("/#warmup", status_code=303)


@app.post("/warmup/{task_id}/pause")
def warmup_pause(request: Request, task_id: int):
    require_login(request)
    try:
        warmup.pause_task(task_id)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#warmup")
    return RedirectResponse("/#warmup", status_code=303)


@app.post("/warmup/{task_id}/resume")
def warmup_resume(request: Request, task_id: int):
    require_login(request)
    try:
        warmup.resume_task(task_id)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#warmup")
    return RedirectResponse("/#warmup", status_code=303)


@app.post("/warmup/{task_id}/delete")
def warmup_delete(request: Request, task_id: int):
    require_login(request)
    try:
        warmup.delete_task(task_id)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#warmup")
    return RedirectResponse("/#warmup", status_code=303)


@app.post("/warmup/review/{schedule_id}/resolve")
def warmup_review_resolve(request: Request, schedule_id: int, resolution: str = Form(...)):
    require_admin(request)
    try:
        if resolution not in {"accepted", "failed"}:
            raise ValueError("无效的处理结果。")
        warmup.resolve_review(schedule_id, resolution)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#warmup")
    return RedirectResponse("/#warmup", status_code=303)


@app.post("/tasks/create")
def tasks_create(
    request: Request,
    tag_id: int = Form(...),
    channel_id: int = Form(...),
    name: str = Form(...),
    subject_template: str = Form(...),
    template_group_id: int = Form(...),
):
    require_login(request)
    try:
        services.create_mail_task(tag_id, channel_id, name, subject_template, template_group_id)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#tasks")
    return RedirectResponse("/#tasks", status_code=303)


@app.post("/tasks/{task_id}/generate")
def generate_task(request: Request, task_id: int):
    require_login(request)
    try:
        services.generate_plan(task_id, force=False)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#tasks")
    return RedirectResponse("/#schedule", status_code=303)


@app.post("/tasks/{task_id}/regenerate")
def regenerate_task(request: Request, task_id: int):
    require_login(request)
    try:
        services.generate_plan(task_id, force=True)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#tasks")
    return RedirectResponse("/#schedule", status_code=303)


@app.post("/tasks/{task_id}/start")
def start_task(request: Request, task_id: int):
    require_login(request)
    services.start_task(task_id)
    return RedirectResponse("/#tasks", status_code=303)


@app.post("/tasks/{task_id}/pause")
def pause_task(request: Request, task_id: int):
    require_login(request)
    services.pause_task(task_id)
    return RedirectResponse("/#tasks", status_code=303)


@app.post("/tasks/{task_id}/resume")
def resume_task(request: Request, task_id: int):
    require_login(request)
    services.resume_task(task_id)
    return RedirectResponse("/#tasks", status_code=303)


@app.post("/tasks/{task_id}/delete")
def delete_task(request: Request, task_id: int):
    require_login(request)
    try:
        services.delete_mail_task(task_id)
    except ValueError as exc:
        return _alert_redirect(str(exc), "/#tasks")
    return RedirectResponse("/#tasks", status_code=303)



def _optional_int(value):
    if value in (None, "", "null", "None", "undefined"):
        return None
    return int(value)



@app.get("/api/tags/{tag_id}/detail")
def api_tag_detail(request: Request, tag_id: int):
    require_login(request)
    try:
        detail = services.get_tag_detail(tag_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"success": True, "detail": detail}


@app.get("/api/recipients/pool/rows")
def api_recipient_pool_rows(
    request: Request,
    tag_id: int,
    pool_type: str,
    status: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = 50,
):
    require_login(request)
    try:
        result = services.get_recipient_pool_rows(
            tag_id, pool_type, status, search, page, page_size
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, **result}

@app.get("/api/schedule/detail/dates")
def api_schedule_detail_dates(
    request: Request,
    tag_id: int,
    task_id: int,
    channel_id: int,
    from_email: str,
):
    require_login(request)
    return {
        "success": True,
        "dates": services.get_schedule_detail_dates(tag_id, task_id, channel_id, from_email),
    }


@app.get("/api/schedule/detail/rows")
def api_schedule_detail_rows(
    request: Request,
    tag_id: int,
    task_id: int,
    channel_id: int,
    from_email: str,
    date: str,
):
    require_login(request)
    return {
        "success": True,
        "rows": services.get_schedule_detail_rows(tag_id, task_id, channel_id, from_email, date),
    }


@app.get("/api/logs/detail/dates")
def api_log_detail_dates(request: Request, task_id: str = "", channel_id: str = ""):
    require_login(request)
    task_id_int = _optional_int(task_id)
    channel_id_int = _optional_int(channel_id)
    return {
        "success": True,
        "dates": services.get_log_detail_dates(task_id_int, channel_id_int),
    }


@app.get("/api/logs/detail/rows")
def api_log_detail_rows(request: Request, task_id: str = "", channel_id: str = "", date: str = ""):
    require_login(request)
    task_id_int = _optional_int(task_id)
    channel_id_int = _optional_int(channel_id)
    return {
        "success": True,
        "rows": services.get_log_detail_rows(task_id_int, channel_id_int, date),
    }

@app.get("/api/health")
def health():
    db_ok = False
    db_error = None
    try:
        db_ok = db_healthcheck()
    except Exception as exc:
        db_error = str(exc)[:500]
    worker = worker_status()
    heartbeat_ok = False
    heartbeat_age_seconds = None
    if worker.get("thread_alive") and worker.get("last_heartbeat_at"):
        try:
            heartbeat_age_seconds = max(
                0.0,
                (datetime.now() - datetime.fromisoformat(worker["last_heartbeat_at"])).total_seconds(),
            )
            heartbeat_ok = heartbeat_age_seconds <= max(
                settings.worker_interval_seconds * 3,
                settings.request_timeout_seconds * 2 + 30,
            )
        except (TypeError, ValueError):
            heartbeat_ok = False
    worker["heartbeat_age_seconds"] = heartbeat_age_seconds
    success = bool(db_ok and heartbeat_ok)
    payload = {
        "success": success,
        "service": "sendgrid-web-admin-scheduler",
        "version": "3.1.0",
        "database_ok": db_ok,
        "database_error": db_error,
        "worker": worker,
    }
    return JSONResponse(payload, status_code=200 if success else 503)


@app.post("/api/sendgrid/events")
async def sendgrid_events(request: Request):
    s = get_settings()
    token = request.headers.get("X-INTERNAL-TOKEN") or ""
    if not token and s.allow_webhook_query_token:
        token = request.query_params.get("token") or ""
    if not hmac.compare_digest(token, s.service_token):
        raise HTTPException(status_code=401, detail="Invalid token")

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > s.max_webhook_bytes:
                raise HTTPException(status_code=413, detail="Webhook payload is too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
    body = await request.body()
    if len(body) > s.max_webhook_bytes:
        raise HTTPException(status_code=413, detail="Webhook payload is too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    count = services.record_sendgrid_events(payload)
    return {"success": True, "count": count}


def run():
    s = get_settings()
    port = find_available_port(s.server_host, s.server_port, s.server_port_scan_limit, s.server_auto_increment_port)
    print("")
    print("==========================================")
    print("SendGrid Web Admin Scheduler v3")
    print("==========================================")
    print("URL: http://{}:{}".format(s.server_host, port))
    print("Login: {} / [your ADMIN_PASSWORD]".format(s.admin_username))
    print("==========================================")
    print("")
    uvicorn.run("app.main:app", host=s.server_host, port=port, reload=False, log_level="info")


if __name__ == "__main__":
    run()
