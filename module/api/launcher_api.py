"""启动器本地通道的 HTTP 路由。

外部启动器（alas-launcher）通过回环连接订阅命令流、回报执行结果，并用信任密钥换取
免密登录令牌；系统设置页则查询状态并请求开关 Windows 开机自启。端点路径与报文格式
由外部启动器二进制写死——改名即断连。
"""
import asyncio
import json
import os
import time

from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from module.base.runtime_params import TOKEN_TTL_SECONDS
from module.runtime import launcher_trust
from module.runtime.launcher import is_local_request, launcher_control

#: 信任密钥的失败窗口与上限：窗口内超限即拒绝，避免密钥被反复试探。
SECRET_FAILURE_WINDOW = 300
SECRET_FAILURE_LIMIT = 15
_secret_failures: list[float] = []

#: 命令流空闲多久发一次保活帧（秒）。
STREAM_IDLE_TIMEOUT = 30

#: 托盘「暂停 / 继续」用到的运行时服务，由 create_app 登记。
_runtime = None
#: 托盘暂停时正在运行的实例；继续时按它恢复，为空则启动全部实例。
_paused: list[str] = []


def configure_trust(webui_key: str | None) -> None:
    """登记启动器信任密钥与当前 WebUI 密码；未带密钥启动时免密通道整体关闭。

    Args:
        webui_key: 当前有效的 WebUI 访问密码。
    """
    launcher_trust.configure(os.environ.get(launcher_trust.TRUST_SECRET_ENV), webui_key)


def configure_runtime(runtime) -> None:
    """登记托盘暂停 / 继续调度器所用的 RuntimeService。"""
    global _runtime
    _runtime = runtime


def _forbidden(message: str, status_code: int = 403) -> JSONResponse:
    """本机限定端点的拒绝响应。"""
    return JSONResponse({"success": False, "error": message}, status_code=status_code)


def _secret_try_allowed() -> bool:
    """失败窗口内是否仍允许尝试。"""
    now = time.time()
    while _secret_failures and now - _secret_failures[0] > SECRET_FAILURE_WINDOW:
        _secret_failures.pop(0)
    return len(_secret_failures) < SECRET_FAILURE_LIMIT


async def status(request):
    """GET /api/launcher/status — 启动器连接与开机自启状态。"""
    if not is_local_request(request):
        return _forbidden("启动器状态只允许本机读取")
    return JSONResponse(launcher_control.status(request_local=True))


async def startup(request):
    """POST /api/launcher/startup — 请求启动器设置 Windows 开机自启。"""
    if not is_local_request(request):
        return _forbidden("开机自启动只能从本机 WebUI 设置")
    try:
        data = await request.json()
    except Exception:
        return _forbidden("请求体不是有效 JSON", 400)
    if not isinstance(data.get("enabled"), bool):
        return _forbidden("enabled 必须是布尔值", 400)

    result = await launcher_control.set_autostart(data["enabled"])
    return JSONResponse(result, status_code=200 if result.get("success") else 500)


async def stream(request):
    """GET /api/launcher/stream — 启动器订阅的本地命令流（SSE）。"""
    if not is_local_request(request):
        return _forbidden("启动器命令流只允许本机连接")

    async def events():
        await launcher_control.mark_connected()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    command = await asyncio.wait_for(launcher_control.next_command(), timeout=STREAM_IDLE_TIMEOUT)
                    launcher_control.keep_alive()
                    yield launcher_control.event(command)
                except asyncio.TimeoutError:
                    launcher_control.keep_alive()
                    yield launcher_control.keepalive_event()
        finally:
            launcher_control.mark_disconnected()

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def report(request):
    """POST /api/launcher/report — 启动器回报命令执行结果。"""
    if not is_local_request(request):
        return _forbidden("启动器回报只允许本机连接")
    try:
        data = await request.json()
    except Exception:
        return _forbidden("请求体不是有效 JSON", 400)
    return JSONResponse(await launcher_control.report(data))


async def trusted_login(request):
    """POST /api/launcher/trusted-login — 启动器以信任密钥换取一次性免密令牌。"""
    if not is_local_request(request):
        return _forbidden("免密通道只允许本机连接")
    if not launcher_trust.enabled():
        return _forbidden("启动器免密通道未启用")
    if not _secret_try_allowed():
        return _forbidden("尝试次数过多，请稍后再试", 429)

    if not launcher_trust.check_secret(request.headers.get("x-webui-launcher-secret")):
        _secret_failures.append(time.time())
        return _forbidden("信任密钥无效")

    token = launcher_trust.issue_token()
    if token is None:
        return _forbidden("免密令牌签发失败")
    return JSONResponse({"success": True, "token": token, "ttl": TOKEN_TTL_SECONDS})


async def login_seed(request):
    """GET /launcher-login?token= — 用一次性令牌把登录凭据写进本机窗口并跳回首页。"""
    if not is_local_request(request) or not launcher_trust.validate_token(request.query_params.get("token")):
        return _login_denied()

    key = launcher_trust.webui_key()
    if not key:
        return HTMLResponse('<!doctype html><script>location.replace("/");</script>')

    # 与控制台同源，写入它读取的凭据键即免密进入；密码内嵌为 JS 字面量并转义 "<"，
    # 免得密码里出现 </script> 提前闭合脚本块。
    literal = json.dumps(str(key)).replace("<", "\\u003c")
    html = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>AzurPilot 免密登录</title><script>'
            f"localStorage.setItem('azurpilot.access-password', {literal});"
            "location.replace('/');</script>"
            '<body>正在免密登录，请稍候…</body></html>')
    return HTMLResponse(html)


def _login_denied() -> HTMLResponse:
    """令牌缺失、失效或非本机访问时的拒绝页。"""
    return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                        '<title>拒绝访问</title><body>该链接无效或已过期，请从启动器重新打开。</body></html>',
                        status_code=403)


def _running_names() -> list[str]:
    """当前在运行的实例名。"""
    from module.runtime.process_manager import ProcessManager
    return sorted({manager.config_name for manager in ProcessManager.running_instances()})


def _scheduler_state() -> dict:
    """托盘菜单需要的调度器状态。"""
    return {"success": True, "running": _running_names(), "paused": list(_paused)}


def _scheduler_action(action: str) -> dict:
    """在线程池里执行暂停或继续，返回最新状态；单个实例失败不影响其余实例。"""
    global _paused
    errors = []
    if action == "pause":
        names = _running_names()
        for name in names:
            try:
                _runtime.stop(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        if names:
            _paused = names
    else:
        running = set(_running_names())
        names = [name for name in (_paused or _runtime.configs.names()) if name not in running]
        for name in names:
            try:
                _runtime.start(name)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        _paused = []
    result = _scheduler_state()
    if errors:
        result.update(success=False, error="；".join(errors))
    return result


async def scheduler(request):
    """GET / POST /api/launcher/scheduler — 托盘菜单查询、暂停或继续调度器。

    只认本机连接且带启动器信任密钥的请求，网页无法跨站触发。POST 报文为
    ``{"action": "pause" | "resume"}``：暂停会停止所有运行中的实例并记住它们，
    继续时重新启动这些实例。
    """
    if not is_local_request(request):
        return _forbidden("调度器控制只允许本机连接")
    if not launcher_trust.check_secret(request.headers.get("x-webui-launcher-secret")):
        return _forbidden("信任密钥无效")
    if _runtime is None:
        return _forbidden("调度器控制未启用", 503)
    if request.method == "GET":
        return JSONResponse(await run_in_threadpool(_scheduler_state))
    try:
        data = await request.json()
    except Exception:
        return _forbidden("请求体不是有效 JSON", 400)
    action = data.get("action") if isinstance(data, dict) else None
    if action not in ("pause", "resume"):
        return _forbidden("action 必须是 pause 或 resume", 400)
    result = await run_in_threadpool(_scheduler_action, action)
    return JSONResponse(result, status_code=200 if result.get("success") else 500)


def routes() -> list[Route]:
    """启动器通道的本地回环路由。"""
    return [Route("/api/launcher/status", status),
            Route("/api/launcher/startup", startup, methods=["POST"]),
            Route("/api/launcher/stream", stream),
            Route("/api/launcher/report", report, methods=["POST"]),
            Route("/api/launcher/trusted-login", trusted_login, methods=["POST"]),
            Route("/api/launcher/scheduler", scheduler, methods=["GET", "POST"]),
            Route("/launcher-login", login_seed)]
