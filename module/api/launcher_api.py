"""启动器独立 WS 入口：回环鉴权、严格方法分发与有界收发。"""
import asyncio
import json
import time
from collections import deque

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from module.api.launcher_protocol import INCOMING_METHODS, LauncherRequest, LauncherResponse
from module.api.protocol import ApiError
from module.runtime.launcher import is_local_request

#: 托盘「暂停 / 继续」用到的运行时服务，由 create_app 登记。
_runtime = None
#: 托盘暂停时正在运行的实例；继续时按它恢复，为空则启动全部实例。
_paused: list[str] = []
#: scheduler() 校验信任密钥所需的 LauncherTrust，由 routes() 登记。
_trust = None


def configure_runtime(runtime) -> None:
    """登记托盘暂停 / 继续调度器所用的 RuntimeService。"""
    global _runtime
    _runtime = runtime


def _forbidden(message: str, status_code: int = 403) -> JSONResponse:
    """本机限定端点的拒绝响应。"""
    return JSONResponse({"success": False, "error": message}, status_code=status_code)


class LauncherGateway:
    def __init__(self, service):
        self.service = service
        self.failures = deque(maxlen=15)

    async def endpoint(self, ws):
        if not is_local_request(ws):
            await ws.close(code=1008)
            return
        now = time.monotonic()
        while self.failures and now - self.failures[0] > 300:
            self.failures.popleft()
        authorization = ws.headers.get('authorization', '')
        if (len(self.failures) >= 15 or not authorization.startswith('Bearer ')
                or not self.service.trust.check_secret(authorization[7:])):
            if len(self.failures) < 15:
                self.failures.append(now)
            await ws.close(code=1008)
            return
        await ws.accept()
        connection = await self.service.attach(ws)
        writer = asyncio.create_task(self.writer(ws, connection))
        received = deque(maxlen=128)
        request_times = deque()
        try:
            while self.service.connection is connection:
                frame = await asyncio.wait_for(ws.receive(), timeout=45 if connection.ready else 5)
                if self.service.connection is not connection:
                    break
                if frame['type'] == 'websocket.disconnect':
                    break
                raw = frame.get('text')
                if raw is None:
                    await ws.close(code=1003)
                    break
                if len(raw.encode('utf-8')) > 65536:
                    await ws.close(code=1009)
                    break
                connection.last_seen = time.monotonic()
                request_times.append(connection.last_seen)
                while request_times and connection.last_seen - request_times[0] >= 1:
                    request_times.popleft()
                if len(request_times) > 30:
                    await ws.close(code=1008)
                    break
                request_id = ''
                replay = False
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict) and isinstance(data.get('id'), str):
                        request_id = data['id'][:100]
                    if isinstance(data, dict) and data.get('type') == 'response':
                        if not connection.ready:
                            raise ApiError('UNAUTHORIZED', '请先完成握手')
                        self.service.complete(connection, LauncherResponse.model_validate(data))
                        continue
                    request = LauncherRequest.model_validate(data)
                    if request.id in received:
                        raise ApiError('DUPLICATE_REQUEST', '请求 ID 已使用')
                    received.append(request.id)
                    model = INCOMING_METHODS.get(request.method)
                    if model is None:
                        raise ApiError('METHOD_NOT_FOUND', '未知启动器方法')
                    params = model.model_validate(request.params)
                    if request.method == 'session.hello':
                        result = self.service.hello(connection, params)
                        replay = True
                    elif not connection.ready:
                        raise ApiError('UNAUTHORIZED', '请先完成握手')
                    elif request.method == 'state.report':
                        result = self.service.report_state(params)
                    elif request.method == 'notifications.ack':
                        result = self.service.acknowledge(params.ids)
                    elif request.method == 'login.issue':
                        result = self.service.trust.issue()
                    else:
                        result = {'pong': True}
                    message = {'type': 'response', 'id': request.id, 'ok': True, 'result': result}
                except (ValidationError, ValueError, TypeError):
                    message = self.failure(request_id, 'INVALID_PARAMS', '启动器报文或参数无效')
                except ApiError as error:
                    message = self.failure(request_id, error.code, error.message)
                self.service._enqueue(connection, message)
                if replay:
                    self.service.replay(connection)
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            self.service.detach(connection)
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
            if ws.application_state.name != 'DISCONNECTED' and ws.client_state.name != 'DISCONNECTED':
                await ws.close(code=1001)

    async def writer(self, ws, connection):
        try:
            while True:
                message = await connection.queue.get()
                if self.service.should_send(message):
                    await asyncio.wait_for(ws.send_json(message), timeout=5)
        except (WebSocketDisconnect, asyncio.TimeoutError, OSError):
            self.service.detach(connection)
            if ws.application_state.name != 'DISCONNECTED' and ws.client_state.name != 'DISCONNECTED':
                await ws.close(code=1013)

    @staticmethod
    def failure(request_id, code, message):
        return {'type': 'response', 'id': request_id, 'ok': False, 'error': {'code': code, 'message': message}}


def routes(service):
    global _trust
    _trust = service.trust
    return [WebSocketRoute('/api/v2/launcher/ws', LauncherGateway(service).endpoint),
            Route('/api/launcher/scheduler', scheduler, methods=['GET', 'POST']),
            Route('/api/launcher/{path:path}', removed, methods=['GET', 'POST']),
            Route('/launcher-login', removed)]


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
    if _trust is None or not _trust.check_secret(request.headers.get("x-webui-launcher-secret")):
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


async def removed(request):
    """旧协议明确 404，不能落入 SPA 回退并返回网页。"""
    return Response(status_code=404)
