import time

import astrbot.api.message_components as Comp
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger


class InputLimiter(Star):
    """输入长度限制插件：按人格规则列表生效，截断超长文本，节约 token。

    截断时同时改写 event.message_str / event.message_obj.message_str /
    event.message_obj.message 三处，确保 LLM 无论读哪一份都拿到截断后的内容。
    提供 /inputlimiter 诊断指令 + debug_log 开关，使生效情况可观测。
    """

    # 人格识别结果缓存（按 umo），避免每条消息都查 3 次数据库。
    # TTL 取 8 秒：/persona 切换人格后最坏 8 秒内生效。
    _PERSONA_CACHE_TTL = 8.0
    _PERSONA_CACHE_MAX = 1000

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self._persona_cache: dict = {}  # umo -> (persona_name, monotonic_ts)

    # ─── 配置读取 ─────────────────────────────────────────────

    def _debug(self) -> bool:
        return bool(self.config.get("debug_log", False))

    def _rules(self) -> list:
        rules = self.config.get("rules", []) or []
        return [r for r in rules if isinstance(r, dict)]

    def _log(self, msg: str):
        if self._debug():
            logger.info(f"[InputLimiter] {msg}")

    def _match_rule(self, persona_name: str):
        if not persona_name:
            return None
        key = persona_name.strip().lower()
        for r in self._rules():
            p = str(r.get("persona", "")).strip()
            if p and p.lower() == key:
                return r
        return None

    # ─── 识别当前生效人格 ─────────────────────────────────────

    async def _resolve_persona_name(self, event: AstrMessageEvent) -> str:
        umo = getattr(event, "unified_msg_origin", "") or ""
        if not umo:
            return ""

        now = time.monotonic()
        cached = self._persona_cache.get(umo)
        if cached and now - cached[1] < self._PERSONA_CACHE_TTL:
            return cached[0]

        name = await self._resolve_persona_name_uncached(event, umo)

        if len(self._persona_cache) >= self._PERSONA_CACHE_MAX:
            self._persona_cache.clear()
        self._persona_cache[umo] = (name, now)
        return name

    async def _resolve_persona_name_uncached(self, event: AstrMessageEvent, umo: str) -> str:
        pm = getattr(self.context, "persona_manager", None)
        cm = getattr(self.context, "conversation_manager", None)

        conv_persona_id = None
        try:
            if cm:
                cid = await cm.get_curr_conversation_id(umo)
                if cid:
                    conv = await cm.get_conversation(umo, cid)
                    conv_persona_id = getattr(conv, "persona_id", None)
        except Exception as e:
            self._log(f"读取会话人格异常: {e}")

        provider_settings = None
        try:
            acm = getattr(self.context, "astrbot_config_mgr", None)
            if acm:
                cfg = acm.get_conf(umo)
                provider_settings = cfg.get("provider_settings") if cfg else None
        except Exception as e:
            self._log(f"读取配置文件异常: {e}")

        try:
            if pm and hasattr(pm, "resolve_selected_persona"):
                res = await pm.resolve_selected_persona(
                    umo=umo,
                    conversation_persona_id=conv_persona_id,
                    platform_name=event.get_platform_name(),
                    provider_settings=provider_settings,
                )
                if res and res[0]:
                    return str(res[0])
        except Exception as e:
            self._log(f"resolve_selected_persona 异常: {e}")

        try:
            if pm and hasattr(pm, "get_default_persona_v3"):
                dp = await pm.get_default_persona_v3(umo)
                name = getattr(dp, "name", None) or (dp.get("name") if isinstance(dp, dict) else None)
                if name:
                    return str(name)
        except Exception as e:
            self._log(f"get_default_persona_v3 异常: {e}")

        return ""

    # ─── 截断（三处字段全改）─────────────────────────────────

    def _apply_truncate(self, event: AstrMessageEvent, max_length: int, suffix: str) -> bool:
        message_chain = event.get_messages()
        if not message_chain:
            return False

        total_text_len = sum(
            len(comp.text) for comp in message_chain if isinstance(comp, Comp.Plain)
        )
        if total_text_len <= max_length:
            self._log(f"文本 {total_text_len} 字 <= 上限 {max_length}，无需截断")
            return False

        remaining = max_length
        new_chain = []
        for comp in message_chain:
            if isinstance(comp, Comp.Plain):
                if remaining <= 0:
                    continue
                if len(comp.text) <= remaining:
                    new_chain.append(comp)
                    remaining -= len(comp.text)
                else:
                    new_chain.append(Comp.Plain(text=comp.text[:remaining] + suffix))
                    remaining = 0
            else:
                new_chain.append(comp)

        new_str = "".join(c.text for c in new_chain if isinstance(c, Comp.Plain))

        try:
            event.message_obj.message = new_chain
        except Exception as e:
            self._log(f"改写 message_obj.message 异常: {e}")
        try:
            event.message_obj.message_str = new_str
        except Exception as e:
            self._log(f"改写 message_obj.message_str 异常: {e}")
        try:
            event.message_str = new_str
        except Exception as e:
            self._log(f"改写 event.message_str 异常: {e}")

        self._log(f"已截断 {total_text_len} -> {len(new_str)} 字符")
        return True

    # ─── 核心拦截 ─────────────────────────────────────────────

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def limit_input(self, event: AstrMessageEvent):
        rules = self._rules()
        if not rules:
            return

        # 斜杠指令（如 /persona、/inputlimiter）不截断，避免截短后指令无法识别
        text = (event.message_str or "").strip()
        if text.startswith("/") or text.startswith("／"):
            return

        persona_name = await self._resolve_persona_name(event)
        rule = self._match_rule(persona_name)

        if not rule:
            self._log(f"人格=「{persona_name or '(空)'}」 无匹配规则，放行")
            return
        if not rule.get("enabled", False):
            self._log(f"人格=「{persona_name}」 命中规则但 enabled=False，放行")
            return

        if rule.get("skip_admin", False) and event.is_admin():
            self._log(f"人格=「{persona_name}」 管理员豁免，放行")
            return

        try:
            max_length = int(rule.get("max_length", 100))
        except (TypeError, ValueError):
            max_length = 100
            self._log(
                f"人格=「{persona_name}」 max_length 配置非法 "
                f"({rule.get('max_length')!r})，已回退为 100"
            )
        if max_length <= 0:
            return

        suffix = rule.get("suffix", "...(已截断)")
        self._log(f"人格=「{persona_name}」 命中且启用，上限 {max_length}，开始判断截断")
        self._apply_truncate(event, max_length, suffix)

    # ─── 诊断指令 ─────────────────────────────────────────────

    @filter.command("inputlimiter")
    async def inputlimiter_diag(self, event: AstrMessageEvent):
        """诊断：查看规则、当前会话人格名、命中情况（仅 AstrBot 全局管理员可用）"""
        if not event.is_admin():
            return
        rules = self._rules()
        lines = [f"🔧 InputLimiter 诊断 | debug_log={'开' if self._debug() else '关'}"]
        lines.append(f"规则数: {len(rules)}")
        for i, r in enumerate(rules, 1):
            lines.append(
                f"  [{i}] 人格=「{r.get('persona', '(空)')}」 "
                f"enabled={r.get('enabled', False)} "
                f"max={r.get('max_length', 100)} "
                f"skip_admin={r.get('skip_admin', False)}"
            )
        if not rules:
            lines.append("  ⚠️ 没有任何规则 -> 插件不会对任何会话生效，请先添加规则。")

        persona_name = await self._resolve_persona_name(event)
        lines.append(f"当前会话识别到的人格名: 「{persona_name or '(空/识别失败)'}」")

        rule = self._match_rule(persona_name)
        if not rules:
            lines.append("命中: 无规则")
        elif not rule:
            lines.append(
                "命中: ❌ 无匹配。请确认上面『识别到的人格名』与某条规则的『人格』完全一致。"
                "若识别为 default 而你想按配置文件区分，说明人格维度区分不开，需改用群号/账号白名单方案。"
            )
        elif not rule.get("enabled", False):
            lines.append("命中: ⚠️ 匹配到规则，但 enabled=False，请在该规则里打开『启用该规则』。")
        else:
            lines.append(f"命中: ✅ 规则[{rule.get('persona')}] 已启用，上限 {rule.get('max_length', 100)} 字")

        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        pass
