"""WebUI会话外壳"""

import json

from module.webui.app_dependencies import (
    AzurLaneConfig,
    Icon,
    ProcessManager,
    State,
    Switch,
    alas_instance,
    clear,
    current_time,
    datetime,
    filepath_args,
    logger,
    put_buttons,
    put_html,
    put_icon_buttons,
    put_loading_text,
    put_scope,
    queue,
    read_file,
    run_js,
    t,
    time,
    time_source_status,
    timedelta,
    timezone,
    updater,
    use_scope,
    webconfig,
)


from module.webui.app_types import WebUIMixinBase


VALID_WEBUI_THEMES = {
    "default",
    "dark",
    "light",
    "advanced_material",
    "dark_advanced_material",
}


def normalize_webui_theme(theme: str) -> str:
    """归一化历史主题名称，并为未知值回退到默认主题。"""
    if theme == "apple":
        return "advanced_material"
    if theme not in VALID_WEBUI_THEMES:
        return "default"
    return theme


def pywebio_theme_for(theme: str) -> str:
    """返回与 AzurPilot 主题匹配的 PyWebIO Bootstrap 主题。"""
    return "dark" if normalize_webui_theme(theme) == "dark" else "default"


def branch_is_unstable(branch) -> bool:
    """分支不在稳定分支（master/main）列表内时视为未经验证。

    None、空串与纯空白都回退为稳定，避免配置缺失时误触发水印。
    """
    branch = (branch or "").strip().lower()
    if not branch:
        return False
    return branch not in ("master", "main")


# 水印层与其样式表的 DOM id，注入与移除共用（关闭水印后需要移除已注入的节点）。
BRANCH_WATERMARK_BOX_ID = "alas-branch-watermark"
BRANCH_WATERMARK_CSS_ID = "alas-branch-watermark-style"

# 未验证分支水印的主提醒文案，中英各一行、同时展示。元信息统一使用 ASCII 标签。
BRANCH_WATERMARK_NOTICE = "您正在使用未经验证的版本，可能存在未知问题"
BRANCH_WATERMARK_NOTICE_EN = (
    "You are using an unverified version, unknown issues may occur"
)
# 版本标识前缀，形如 ``Ver.<分支名>.<版本哈希>``。
BRANCH_WATERMARK_VERSION_TAG = "Ver"
# 分支标识前缀，形如 ``Branche is:<分支名>``。
BRANCH_WATERMARK_BRANCH_TAG = "Branche is"

# 水印字体：英文与数字统一走 JetBrains Mono NL，中文回退到界面主字体。
# 注意 alas.css 里有 `body *:not(...) { font-family: 'MiSans' ... !important }`
# 这条全局规则，其特异性(id 计数 2)高于本文件的 id+class 选择器，因此纯 CSS
# 声明会被压掉，必须靠 JS 内联 `!important` 才能真正生效。
BRANCH_WATERMARK_FONT_STACK = (
    "'JetBrains Mono NL', 'MiSans', \"Microsoft YaHei\", sans-serif"
)

# 水印格内各行文本的裁剪上限。水印是 nowrap 平铺的，分支名/提交信息过长会
# 撑破格子并互相重叠，这里按字符数截断并用省略号收尾。
BRANCH_WATERMARK_MAX_BRANCH_LEN = 28
BRANCH_WATERMARK_MAX_VERSION_LEN = 16
BRANCH_WATERMARK_MAX_MESSAGE_LEN = 40

# 水印行的展示类型，供 CSS 区分主提醒（中文为主、英文为副）、元信息。
BRANCH_WATERMARK_KIND_TITLE = "title"
BRANCH_WATERMARK_KIND_TITLE_EN = "title-en"
BRANCH_WATERMARK_KIND_META = "meta"

# 读不到分支（部署配置读取失败、git 也拿不到）时使用的占位名，按未经验证处理。
BRANCH_WATERMARK_UNKNOWN_BRANCH = "unknown"


def resolve_watermark_branch(branch=None) -> str:
    """规范化水印使用的分支名，取值无效时返回占位名。

    ``git rev-parse --abbrev-ref HEAD`` 在游离头指针时返回 ``HEAD``；空值与 ``HEAD``
    都不能当作「已验证分支」，统一替换为 BRANCH_WATERMARK_UNKNOWN_BRANCH。

    Args:
        branch: 部署配置或 git 给出的分支名。

    Returns:
        str: 可用于水印展示的分支名。
    """
    branch = str(branch or "").strip()
    if not branch or branch.upper() == "HEAD":
        return BRANCH_WATERMARK_UNKNOWN_BRANCH
    return branch


def branch_needs_watermark(branch) -> bool:
    """是否需要注入未验证版本水印。

    与 ``branch_is_unstable()`` 的区别：无法确定分支（占位名或空值）时按未经验证
    处理。水印是排查问题的诊断信息，配置读不到时更应该显示，不能静默隐藏；而
    ``branch_is_unstable()`` 对空值返回 False 的既有契约保持不变（它只回答
    「这个分支名是否属于稳定分支」）。

    Args:
        branch: 分支名。

    Returns:
        bool: True 表示应当注入水印。
    """
    branch = str(branch or "").strip()
    if not branch or branch.lower() == BRANCH_WATERMARK_UNKNOWN_BRANCH:
        return True
    return branch_is_unstable(branch)


def detect_git_branch() -> str:
    """读取当前工作区实际的 git 分支名，失败返回空串。

    仅作为部署配置不可用时的兜底：配置坏了也应尽量展示真实的构建信息。

    Returns:
        str: 分支名；读取失败或游离头指针时返回空串。
    """
    try:
        log = updater.execute_output(f'"{updater.git}" rev-parse --abbrev-ref HEAD')
    except Exception as e:
        logger.warning(f"读取当前 git 分支失败: {e}")
        return ""
    branch = (log or "").strip()
    if branch.upper() == "HEAD":
        return ""
    return branch


def branch_watermark_disabled(config) -> bool:
    """部署配置是否要求关闭未验证版本水印。

    默认（配置缺失、属性不存在或读取异常）返回 False，即保持显示水印；
    只有用户在 WebUI 设置里显式打开「关闭未经验证版本的水印」时才返回 True。
    水印里的分支名、版本哈希与提交信息是判断实际运行代码的唯一线索，
    因此关闭与否只由用户显式配置决定，不做任何隐式推断。

    Args:
        config: DeployConfig 实例，或任何可能带 DisableBranchWatermark 的对象。

    Returns:
        bool: True 表示应当隐藏水印。
    """
    try:
        value = getattr(config, "DisableBranchWatermark", False)
    except Exception:
        return False
    # 只认真正的布尔 True：deploy.yaml 里误写成字符串（"false" / "0" / "no"）
    # 或数字 1 都不算开启，避免格式错误的值被当成真、把水印静默关掉。
    return value is True


def _clip_watermark_text(text, limit: int) -> str:
    """把任意文本压成单行并按字符数裁剪，超长部分用省略号收尾。"""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def build_branch_watermark_lines(branch, commit=None) -> list:
    """构造未验证分支水印的文案行。

    开头是中英双语的提醒（中文为主、英文为副），其后为 ASCII 元信息：
    - 主提醒 ``您正在使用未经验证的版本，可能存在未知问题``
    - 英文提醒 ``You are using an unverified version, unknown issues may occur``
    - 版本标识 ``Ver.<分支名>.<版本哈希>``
    - 分支标识 ``Branche is:<分支名>``
    - 提交内容（裸写，不加前缀）

    Args:
        branch: 当前部署分支名，例如 dev / feature/new。
        commit: ``updater.get_commit(short_sha1=True)`` 的返回值
            ``(sha1, author, isotime, message)``。读取失败或为空时，
            版本哈希回退为 ``unknown``，并省略提交行。

    Returns:
        ``[{"text": ..., "kind": "title"|"title-en"|"meta"}, ...]``
    """
    lines = [
        {"text": BRANCH_WATERMARK_NOTICE, "kind": BRANCH_WATERMARK_KIND_TITLE},
        {"text": BRANCH_WATERMARK_NOTICE_EN, "kind": BRANCH_WATERMARK_KIND_TITLE_EN},
    ]

    sha1 = message = None
    if commit:
        sha1 = commit[0] if len(commit) > 0 else None
        message = commit[3] if len(commit) > 3 else None

    branch_name = _clip_watermark_text(branch, BRANCH_WATERMARK_MAX_BRANCH_LEN)
    version = _clip_watermark_text(sha1, BRANCH_WATERMARK_MAX_VERSION_LEN) or "unknown"

    ver_segments = [BRANCH_WATERMARK_VERSION_TAG]
    if branch_name:
        ver_segments.append(branch_name)
    ver_segments.append(version)
    lines.append(
        {
            "text": ".".join(ver_segments),
            "kind": BRANCH_WATERMARK_KIND_META,
        }
    )

    if branch_name:
        lines.append(
            {
                "text": f"{BRANCH_WATERMARK_BRANCH_TAG}:{branch_name}",
                "kind": BRANCH_WATERMARK_KIND_META,
            }
        )

    subject = _clip_watermark_text(message, BRANCH_WATERMARK_MAX_MESSAGE_LEN)
    if subject:
        lines.append({"text": subject, "kind": BRANCH_WATERMARK_KIND_META})

    return lines


# 未验证分支水印的样式：低调淡灰、pointer-events 穿透，避免影响观感与操作。
# z-index 与首屏骨架同级（低于更新提示 2147483647）；暗色主题通过 body 上的
# webio-theme-dark 选择器适配，切换主题时颜色自动跟随，无需重新注入。
BRANCH_WATERMARK_CSS = """
#alas-branch-watermark{
    position: fixed;
    left: 0;
    top: 0;
    width: 100%;
    height: 100%;
    z-index: 2147483000;
    pointer-events: none;
    overflow: hidden;
}
/* 英文与数字统一用 JetBrains Mono NL，中文回退界面主字体。这条 !important
   仍会被 alas.css 的全局 `body *:not(...) !important` 规则按特异性压掉，
   实际生效靠注入 JS 里的内联 !important；此处保留以便全局规则变动时兜底。 */
#alas-branch-watermark,
#alas-branch-watermark .alas-wm-cell,
#alas-branch-watermark .alas-wm-cell span{
    font-family: 'JetBrains Mono NL', 'MiSans', "Microsoft YaHei",
        sans-serif !important;
}
#alas-branch-watermark .alas-wm-cell{
    position: absolute;
    width: 540px;
    height: 340px;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    justify-content: center;
    transform: rotate(-18deg);
    transform-origin: center center;
    white-space: nowrap;
}
#alas-branch-watermark .alas-wm-cell span{
    display: block;
    user-select: none;
    pointer-events: none;
}
#alas-branch-watermark .alas-wm-cell span.alas-wm-title{
    font-size: 18px;
    font-weight: 600;
    line-height: 2.2;
    color: rgba(90, 90, 90, .34);
}
#alas-branch-watermark .alas-wm-cell span.alas-wm-title-en{
    font-size: 15px;
    font-weight: 500;
    line-height: 2.0;
    color: rgba(100, 100, 100, .30);
}
#alas-branch-watermark .alas-wm-cell span.alas-wm-meta{
    font-size: 13px;
    font-weight: 500;
    line-height: 1.9;
    color: rgba(120, 120, 120, .28);
}
body.webio-theme-dark #alas-branch-watermark .alas-wm-cell span.alas-wm-title{
    color: rgba(210, 210, 210, .26);
}
body.webio-theme-dark #alas-branch-watermark .alas-wm-cell span.alas-wm-title-en{
    color: rgba(205, 205, 205, .23);
}
body.webio-theme-dark #alas-branch-watermark .alas-wm-cell span.alas-wm-meta{
    color: rgba(200, 200, 200, .22);
}
"""


def theme_css_source() -> str:
    """返回当前主题的 CSS 文本，供背景注入拼回主题自己的叠加渐变。

    高级黑在 body 上叠了一层深色渐变把背景压暗。自定义背景会直接覆盖
    ``background-image``，不把那层渐变拼回去，深色主题下背景会突然变亮。

    从主题文件读而不是另存一份，渐变值只存在一处，上游改了也不会漂移。
    读不到时返回空串——背景本身还能用，只差那层叠加。
    """
    from module.webui.setting import State
    from module.webui.utils import filepath_css

    # 用全局 State.theme：本函数定义在 AppShellMixin 之前，引类会 NameError
    theme = getattr(State, 'theme', None) or 'default'
    names = {
        'dark_advanced_material': (
            'advanced-material-alas',
            'dark-advanced-material-overrides-alas',
        ),
    }.get(theme, (f'{theme.replace("_", "-")}-alas',)
          if theme not in ('default',) else ('light-alas',))

    parts = []
    for name in names:
        try:
            with open(filepath_css(name), 'r', encoding='utf-8') as f:
                parts.append(f.read())
        except OSError as e:
            logger.warning(f'[WebUI-背景] 读主题 CSS {name} 失败: {e}')
    return '\n'.join(parts)


def _reload_theme_css(theme: str) -> None:
    """切换主题时移除旧主题的 <link> 与 <style>，并重新注入当前主题 CSS。

    初始 HTML 预加载的 <link> 元素（如 dark-alas.css）在切换主题后
    仍残留在 DOM 中，其 !important 规则会覆盖新主题的 CSS。需要先
    删除所有主题 CSS 的 <link> 和 <style>，再注入当前主题的 CSS。
    """
    import json
    from module.webui.app_dependencies import local
    from module.webui.utils import THEME_STYLES, add_css_files, filepath_css, get_all_theme_style_ids

    theme_ids = get_all_theme_style_ids()
    run_js("""
    var links = document.querySelectorAll(
        'link[href*="dark-alas"],' +
        'link[href*="light-alas"],' +
        'link[href*="advanced-material-alas"],' +
        'link[href*="dark-advanced-material"]'
    );
    for (var i = 0; i < links.length; i++) {
        links[i].parentNode.removeChild(links[i]);
    }
    var ids = %s;
    for (var i = 0; i < ids.length; i++) {
        var el = document.getElementById(ids[i]);
        if (el && el.parentNode) {
            el.parentNode.removeChild(el);
        }
    }
    """ % json.dumps(theme_ids))

    injected_styles = getattr(local, "webui_injected_styles", None)
    if injected_styles is not None:
        all_theme_names = {"light-alas"}
        for names in THEME_STYLES.values():
            all_theme_names.update(names)
        for name in all_theme_names:
            injected_styles.discard(filepath_css(name))

    theme_files = THEME_STYLES.get(theme, ("light-alas",))
    add_css_files(filepath_css(name) for name in theme_files)


class AppShellMixin(WebUIMixinBase):
    """WebUI会话外壳"""

    def initial(self) -> None:
        from module.webui.app_cache import get_cached_menu_args

        menu, args = get_cached_menu_args(
            self.alas_mod,
            read_file,
            filepath_args,
        )
        self.ALAS_MENU = menu
        self.ALAS_ARGS = args

    def __init__(self) -> None:
        super().__init__()
        # 在渲染侧边栏前初始化，避免慢加载时实例按钮先触发而访问未定义属性。
        self.state_switch = Switch(
            status=self.set_status,
            get_state=lambda: getattr(getattr(self, "alas", -1), "state", 0),
            name="state",
        )
        # 已修改的配置键，来自 pin_wait_change() 的返回值
        self.modified_config_queue = queue.Queue()
        # 当前 Alas 配置名称
        self.alas_name = ""
        self.alas_mod = "alas"
        self.alas_config = AzurLaneConfig("template")
        self.initial()
        # 已渲染的状态缓存
        self.rendered_cache = []
        self.inst_cache = []
        self._shell_mounted = False
        self._active_aside = None
        self._stored_aside = None
        self._overview_snapshot = None
        self.af_flag = False
        self._last_announcement_id = None
        self._announcement_result = None
        self._announcement_fetching = False
        self._announcement_force = False
        self._update_notified = False
        self._simulator = None
        self._simulator_logger_pm = None
        self._overview_log = None
        self._overview_log_config_name = None
        self._statistics_cache_key = None
        self._statistics_source_signature = None
        self._statistics_refresh_pending = False

    @property
    def simulator(self):
        """在首次进入大世界模拟器时再加载其运行时依赖。"""
        if self._simulator is None:
            import sys

            from module.webui.fake_pil_module import remove_fake_pil_module

            # matplotlib 需要真实 PIL；仅移除 WebUI 启动阶段安装的替身，
            # 避免其他会话已加载真实 PIL 时再次从模块缓存中删除它。
            if not hasattr(sys.modules.get("PIL"), "__path__"):
                remove_fake_pil_module()
            from module.os_simulator.simulator import OSSimulator

            self._simulator = OSSimulator()
        return self._simulator

    def _close_update_notice(self) -> None:
        run_js(
            r"""
            (function () {
                var el = document.getElementById('alas-update-notice');
                if (!el) return;
                el.classList.add('is-leaving');
                setTimeout(function () {
                    if (el && el.parentNode) {
                        el.parentNode.removeChild(el);
                    }
                }, 180);
            })();
            """
        )

    def _remove_update_notice(self) -> None:
        run_js(
            r"""
            (function () {
                var el = document.getElementById('alas-update-notice');
                if (el && el.parentNode) {
                    el.parentNode.removeChild(el);
                }
            })();
            """
        )

    def _show_update_notice(self, onclick) -> None:
        self._remove_update_notice()
        scope = f"update_notice_{int(time.time() * 1000)}"

        def handle_later():
            self._close_update_notice()

        with use_scope("ROOT"):
            put_html(
                f"""
                <div id="alas-update-notice" class="alas-update-notice" role="status" aria-live="polite">
                    <div class="alas-update-notice__halo"></div>
                    <div class="alas-update-notice__icon" aria-hidden="true">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                             stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                            <path d="M7 10l5 5 5-5"></path>
                            <path d="M12 15V3"></path>
                        </svg>
                    </div>
                    <div class="alas-update-notice__body">
                        <div class="alas-update-notice__eyebrow">发现新版本</div>
                        <div class="alas-update-notice__title">有可用更新！</div>
                        <div class="alas-update-notice__text">
                            建议及时更新，以获得更稳定的脚本运行体验。
                        </div>
                        <div id="pywebio-scope-{scope}" class="alas-update-notice__actions"></div>
                    </div>
                </div>
                """
            )
            put_buttons(
                [
                    {
                        "label": "立即更新",
                        "value": "update",
                        "color": "danger",
                    },
                    {
                        "label": "稍后再说",
                        "value": "later",
                        "color": "secondary",
                    },
                ],
                onclick=[onclick, handle_later],
                small=True,
                scope=scope,
            )

    @use_scope("aside", clear=True)
    def set_aside(self) -> None:
        # TODO: 更新 put_icon_buttons()

        # 愚人节装饰只需要本机日历，不应在首屏请求线程同步等待 NTP。
        current_date = datetime.now().date()
        if current_date.month == 4 and current_date.day == 1:
            self.af_flag = True

        put_scope("aside_home")
        put_scope("aside_instance")
        put_scope("aside_manage")
        self.refresh_aside_labels()
        self.refresh_aside_instances(force=True)

    def refresh_aside_labels(self) -> None:
        """语言变化时只更新主边栏中的静态按钮。"""
        with use_scope("aside_home", clear=True):
            put_icon_buttons(
                Icon.DEVELOP,
                "false",
                buttons=[
                    {
                        "label": t("Gui.Aside.Home"),
                        "value": "Home",
                        "color": "aside",
                    }
                ],
                onclick=[self.ui_develop],
            )
        with use_scope("aside_manage", clear=True):
            put_icon_buttons(
                Icon.SETTING,
                "false",
                buttons=[
                    {
                        "label": t("Gui.AddAlas.Manage"),
                        "value": "Manage",
                        "color": "aside",
                    }
                ],
                onclick=[self.ui_manage],
            )
        aside_name = self._active_aside or self._stored_aside or "Home"
        self.active_button("aside", aside_name)

    @use_scope("aside_instance")
    def refresh_aside_instances(self, force=False) -> None:
        """仅在实例集合或运行状态变化时更新实例侧栏。"""
        instances = alas_instance()
        rebuild = (
            force
            or instances != self.inst_cache
            or len(self.rendered_cache) != len(instances)
        )

        def update(name, seq):
            with use_scope(f"alas-instance-{seq}", clear=True):
                rendered_state = ProcessManager.get_manager(name).state
                if rendered_state == 1:
                    icon_html = Icon.RUNNING
                elif rendered_state == 3:
                    icon_html = Icon.ERROR
                elif rendered_state == 4:
                    icon_html = Icon.UPDATE
                else:
                    icon_html = Icon.RUN
                status_signal = "false" if rendered_state in (1, 3, 4) else "true"
                if rendered_state == 1 and getattr(self, "af_flag", False):
                    icon_html = icon_html[:31] + " anim-rotate" + icon_html[31:]
                put_icon_buttons(
                    icon_html,
                    status_signal,
                    buttons=[{"label": name, "value": name, "color": "aside"}],
                    onclick=self.ui_alas,
                )
            return rendered_state

        changed = rebuild
        if rebuild:
            self.inst_cache = instances
            self.rendered_cache.clear()
            clear()
            for index, _ in enumerate(instances):
                put_scope(f"alas-instance-{index}")
            for index, inst in enumerate(instances):
                self.rendered_cache.append(update(inst, index))
        else:
            for index, inst in enumerate(instances):
                state = ProcessManager.get_manager(inst).state
                if state != self.rendered_cache[index]:
                    self.rendered_cache[index] = update(inst, index)
                    changed = True

        if changed:
            aside_name = self._active_aside or self._stored_aside or "Home"
            self.active_button("aside", aside_name)

    def set_aside_status(self) -> None:
        self.refresh_aside_instances()

    @use_scope("header_status")
    def set_status(self, state: int) -> None:
        """
        Args:
            state (int):
                1 (running)
                2 (not running)
                3 (warning, stop unexpectedly)
                4 (stop for update)
                0 (hide)
                -1 (*state not changed)
        """
        if state == -1:
            return
        clear()

        if state == 1:
            put_loading_text(t("Gui.Status.Running"), color="success")
        elif state == 2:
            put_loading_text(t("Gui.Status.Inactive"), color="secondary", fill=True)
        elif state == 3:
            put_loading_text(t("Gui.Status.Warning"), shape="grow", color="warning")
        elif state == 4:
            put_loading_text(t("Gui.Status.Updating"), shape="grow", color="success")

    @staticmethod
    def _format_tz_offset(offset: timedelta) -> str:
        seconds = int(offset.total_seconds())
        sign = "+" if seconds >= 0 else "-"
        seconds = abs(seconds)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60
        return f"UTC{sign}{hours:02d}:{minutes:02d}"

    def _time_status_text(self) -> str:
        data = time_source_status()
        local_offset = current_time(timezone.utc).astimezone().utcoffset()
        local_tz = self._format_tz_offset(local_offset or timedelta(0))
        sync_text = "已同步" if data["synced"] else "本机时间"
        enabled_text = "NTP" if data["enabled"] else "NTP关闭"
        return (
            f"{enabled_text} {sync_text} · 偏移 {data['offset']:+.3f}s · "
            f"本机 {local_tz}"
        )

    @classmethod
    def set_theme(cls, theme="default") -> None:
        theme = normalize_webui_theme(theme)
        cls.theme = theme
        State.deploy_config.Theme = theme
        State.theme = theme

        pywebio_theme = pywebio_theme_for(theme)

        webconfig(theme=pywebio_theme)  

        run_js("""
        document.querySelectorAll(
            'link[href*="advanced-material-alas"],' +
            'link[href*="dark-advanced-material-overrides-alas"]'
        ).forEach(function(e) {
            e.remove();
        });
        """)

        run_js(f"""
        (function() {{
            var link = document.querySelector('link[href*="bs-theme/"]');
            if (link) {{
                link.href = link.href.replace(
                    /bs-theme\\/\\S+\\.min\\.css/,
                    'bs-theme/{pywebio_theme}.min.css'
                );
            }}
            document.body.className = document.body.className
                .replace(/webio-theme-\\S+/g, '')
                + ' webio-theme-{pywebio_theme}';
        }})();
        """)

        # 清空会话注入追踪中的主题 CSS 记录，然后重新调用 load_webui_styles
        # 为当前主题注入正确的 CSS。旧主题残留的 !important 规则会被新 CSS 覆盖。
        _reload_theme_css(theme)

        run_js(f"""
        window.dispatchEvent(
            new CustomEvent(
                "alas-theme-change",
                {{detail: "{theme}"}}
            )
        );
        """)

    @staticmethod
    def _remove_branch_watermark() -> None:
        """移除已注入的水印层与样式表（关闭水印开关后调用）。

        Pages: 会话外壳（登录后任意主界面）
        """
        run_js(f"""
        (function () {{
            ["{BRANCH_WATERMARK_BOX_ID}", "{BRANCH_WATERMARK_CSS_ID}"].forEach(function (id) {{
                var node = document.getElementById(id);
                if (node && node.parentNode) {{
                    node.parentNode.removeChild(node);
                }}
            }});
        }})();
        """)

    def _inject_unverified_branch_watermark(self) -> None:
        """更新分支不是 master/main 时，注入全屏淡灰水印提醒。

        水印除固定提醒文案外，还展示当前分支名、版本哈希与版本提交信息，
        便于快速判断正在运行的是哪一个未验证构建。

        在登录后的会话外壳挂载阶段调用一次。通过 run_js 向 <body> 直挂一个
        fixed 全屏层，不属 PyWebIO scope，切换页面不会被清除；方法幂等，
        浏览器刷新重建会话后会先移除旧节点再重建。

        Pages: 会话外壳（登录后任意主界面）
        """
        branch = ""
        disabled = False
        try:
            State.deploy_config.read()
            branch = getattr(State.deploy_config, "Branch", "") or ""
            disabled = branch_watermark_disabled(State.deploy_config)
        except Exception as e:
            logger.warning(f"读取部署配置失败，改按实际 git 分支判断是否注入水印: {e}")

        if not str(branch).strip():
            # 配置读不到时不能当成已验证的 master：退回实际 git 分支，仍拿不到就
            # 按未知分支处理（fail-safe，见 branch_needs_watermark）。
            branch = detect_git_branch()
        branch = resolve_watermark_branch(branch)

        # 只有非稳定分支（dev / app 等）才需要水印；master / main 视为已验证
        # 分支，本就不显示水印，开关对它们没有意义。
        unstable = branch_needs_watermark(branch)

        if disabled:
            # 用户显式关闭：移除本会话可能已注入的水印层；未验证分支上要留下明确
            # 警告，避免后续用无版本信息的截图反馈问题时无法定位。
            self._remove_branch_watermark()
            if unstable:
                logger.warning(
                    f"未验证分支 {branch} 的水印已按 WebUI 设置关闭"
                    "（WebUI.DisableBranchWatermark=true）；该设置仅限了解各分支用途的用户使用，"
                    "请勿据此截图反馈问题"
                )
            else:
                logger.info(
                    f"水印开关已打开，但当前分支 {branch} 属已验证分支，"
                    "本来就不显示未验证版本水印"
                )
            return

        if not unstable:
            return

        commit = None
        try:
            commit = updater.get_commit(short_sha1=True)
        except Exception as e:
            logger.warning(f"读取本地版本信息失败，水印仅显示分支名: {e}")

        lines = build_branch_watermark_lines(branch, commit)

        run_js(f"""
        (function () {{
            var BOX_ID = {json.dumps(BRANCH_WATERMARK_BOX_ID)};
            var CSS_ID = {json.dumps(BRANCH_WATERMARK_CSS_ID)};

            var oldBox = document.getElementById(BOX_ID);
            if (oldBox && oldBox.parentNode) {{
                oldBox.parentNode.removeChild(oldBox);
            }}
            var oldStyle = document.getElementById(CSS_ID);
            if (oldStyle && oldStyle.parentNode) {{
                oldStyle.parentNode.removeChild(oldStyle);
            }}

            var style = document.createElement("style");
            style.id = CSS_ID;
            style.textContent = {json.dumps(BRANCH_WATERMARK_CSS)};
            document.head.appendChild(style);

            var box = document.createElement("div");
            box.id = BOX_ID;
            box.setAttribute("aria-hidden", "true");

            var lines = {json.dumps(lines, ensure_ascii=False)};
            var fontStack = {json.dumps(BRANCH_WATERMARK_FONT_STACK)};
            var cellW = 540;
            var cellH = 340;
            var vw = window.innerWidth || document.documentElement.clientWidth || 1280;
            var vh = window.innerHeight || document.documentElement.clientHeight || 720;
            var cols = Math.ceil(vw / cellW) + 1;
            var rows = Math.ceil(vh / cellH) + 1;
            for (var r = 0; r < rows; r++) {{
                for (var c = 0; c < cols; c++) {{
                    var cell = document.createElement("div");
                    cell.className = "alas-wm-cell";
                    cell.style.left = (c * cellW) + "px";
                    cell.style.top = (r * cellH) + "px";
                    for (var i = 0; i < lines.length; i++) {{
                        var span = document.createElement("span");
                        span.className = "alas-wm-" + lines[i].kind;
                        span.textContent = lines[i].text;
                        // alas.css 的全局 `body *:not(...) !important` 字体规则
                        // 特异性高于水印样式表，只能用内联 !important 反压。
                        span.style.setProperty(
                            "font-family", fontStack, "important"
                        );
                        cell.appendChild(span);
                    }}
                    box.appendChild(cell);
                }}
            }}

            document.body.appendChild(box);
        }})();
        """)
