import json

import uvicorn
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import get_settings
from .db import init_db
from .utils import find_available_port
from .worker import start_worker_once
from . import services

settings = get_settings()
app = FastAPI(title="SendGrid Web Admin Scheduler", version="3.0.0-py39")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
templates = Jinja2Templates(directory="app/templates")

RECIPIENT_EXTENSIONS = {".txt", ".csv"}
TEMPLATE_EXTENSIONS = {".html", ".htm"}


@app.on_event("startup")
def startup():
    init_db()
    start_worker_once()


def require_login(request: Request):
    if not request.session.get("logged_in"):
        raise HTTPException(status_code=401, detail="Not logged in")
    return request.session.get("user") or {}


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




def _alert_redirect(message, target="/#tasks"):
    return HTMLResponse("""
<!doctype html><meta charset="utf-8"><script>
alert(%s);
location.href = %s;
</script>
""" % (json.dumps(str(message), ensure_ascii=False), json.dumps(target, ensure_ascii=False)))


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


async def _read_unlimited_upload(file: UploadFile, allowed_extensions, label):
    filename = file.filename or ""
    ext = _file_extension(filename)
    if ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail="{} file type is not allowed: {}".format(label, filename or "(unnamed)")
        )
    content = await file.read()
    if not content:
        raise HTTPException(
            status_code=400,
            detail="{} file is empty: {}".format(label, filename or "(unnamed)")
        )
    return content


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not request.session.get("logged_in"):
        return templates.TemplateResponse("login.html", {"request": request, "error": None})
    data = services.get_dashboard_data()
    data["request"] = request
    data["current_user"] = request.session.get("user") or {}
    return templates.TemplateResponse("admin.html", data)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = services.authenticate_user(username, password)
    if user:
        request.session["logged_in"] = True
        request.session["user"] = user
        return RedirectResponse("/", status_code=303)
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
    services.create_user(username, password, display_name, role, status)
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
    services.update_user(user_id, display_name, role, status, password)
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
    return RedirectResponse("/#tags", status_code=303)


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
        return _alert_redirect(result.get("message") or "代理测试完成", "/#proxies")
    return RedirectResponse("/#proxies", status_code=303)


@app.post("/proxies/{proxy_id}/test")
def test_proxy(request: Request, proxy_id: int):
    require_login(request)
    try:
        result = services.test_proxy(proxy_id)
        return _alert_redirect(result.get("message") or "代理测试完成", "/#proxies")
    except Exception as exc:
        return _alert_redirect("代理测试失败：{}".format(exc), "/#proxies")


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
    return RedirectResponse("/#channels", status_code=303)


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
    return RedirectResponse("/#channels", status_code=303)


@app.post("/recipients/upload")
async def recipients_upload(
    request: Request,
    tag_id: int = Form(...),
    pool_type: str = Form(...),
    name: str = Form(""),
    files: list[UploadFile] = File(...),
):
    require_login(request)
    # 收件人池/库上传不再使用 MAX_RECIPIENT_UPLOAD_BYTES 限制，并支持一次多选 TXT/CSV。
    if not files:
        return _alert_redirect("请至少选择一个 TXT/CSV 收件人文件。", "/#recipients")

    summaries = []
    total_parsed = 0
    total_imported = 0
    total_duplicates = 0
    total_invalid = 0
    failed = []

    for file in files:
        filename = file.filename or "未命名文件"
        try:
            content = await _read_unlimited_upload(file, RECIPIENT_EXTENSIONS, "recipient pool")
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
    services.delete_mail_task(task_id)
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
    return {"success": True, "service": "sendgrid-web-admin-scheduler", "version": "3.0.0"}


@app.post("/api/sendgrid/events")
async def sendgrid_events(request: Request):
    s = get_settings()
    token = request.query_params.get("token") or request.headers.get("X-INTERNAL-TOKEN")
    if token != s.service_token:
        raise HTTPException(status_code=401, detail="Invalid token")
    payload = await request.json()
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
