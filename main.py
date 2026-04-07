import asyncio
import re
import os
import json
import inspect
from typing import Dict, List, Set, Optional, Any

# 核心导入
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.core.star.filter.event_message_type import EventMessageType
from .core.permission import PermissionManager

# --- 从我们自己的模块中导入所有“零件” ---
from .api_client import APIClient
from .manager import MemeManager
from .recorder import StatsRecorder

# 导入所有处理器 Mixin
from .handlers.help import HelpHandlers
from .handlers.search import SearchHandlers
from .handlers.management import ManagementHandlers
from .handlers.statistics import StatisticsHandlers
from .handlers.tools import ToolHandlers
from .handlers.generation import GenerationHandlers, UserInGroupSessionFilter
from .handlers.info import InfoHandlers

@register(
    "meme_maker_api", 
    "Meme Bot", 
    "功能完善的表情包与图片工具插件", 
    "5.1.0" # 升级为原生撤回版
)
class MemeMakerApiPlugin(
    Star,
    HelpHandlers,
    SearchHandlers,
    InfoHandlers,
    ManagementHandlers,
    StatisticsHandlers,
    ToolHandlers,
    GenerationHandlers
):
    """
    一个功能强大、高度模块化的表情包制作与图片处理插件。
    """
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.logger = logger
        
        # 1. 加载配置
        self.api_url = self.config.get("meme_generator_base_url", "http://127.0.0.1:2233/")
        if not self.api_url.endswith('/'):
            self.api_url += '/'
        
        main_config = self.context.get_config()
        self.prefix = self.config.get("command_prefix", "-")
        self.superusers: List[str] = [str(uid) for uid in main_config.get("admins_id", [])]
        
        self.timeout = self.config.get("timeout", 20)
        self.fuzzy_match = self.config.get("fuzzy_match", True)
        self.use_sender_when_no_image = self.config.get("use_sender_when_no_image", True)
        self.bot_name = self.config.get("bot_display_name", "Meme Bot")
                
        self.label_new_days = self.config.get("label_new_days", 7)
        self.label_hot_days = self.config.get("label_hot_days", 30)
        self.label_hot_threshold = self.config.get("label_hot_threshold", 20)

        interactive_config = self.config.get("interactive_settings", {})
        self.interactive_enabled = interactive_config.get("enabled", True)
        self.session_timeout = interactive_config.get("timeout", 60)
        recall_config = interactive_config.get("recall", {})
        self.recall_enabled = recall_config.get("enabled", False)

        reprompt_config = interactive_config.get("smart_reprompt", {})
        self.reprompt_enabled = reprompt_config.get("enabled", True)
        self.reprompt_threshold = reprompt_config.get("threshold", 2)
        
        multi_image_config = self.config.get("multi_image_options", {})
        self.direct_send_threshold = multi_image_config.get("direct_send_threshold", 3)
        self.send_forward_msg = multi_image_config.get("send_forward_msg", True)
        self.send_as_zip_enabled = multi_image_config.get("send_as_zip_enabled", True)
        self.zip_threshold = multi_image_config.get("zip_threshold", 20)
        self.zip_use_base64 = multi_image_config.get("zip_use_base64", False)

        # 2. 初始化所有管理器
        self.api_client = APIClient(self.api_url, self.timeout)
        self.meme_manager = MemeManager()
        
        # 【核心修正】使用框架提供的标准方法获取数据目录
        data_dir = StarTools.get_data_dir("meme_maker_api")
        data_dir.mkdir(parents=True, exist_ok=True)  # 使用 pathlib 的方法创建目录
        self.db_path = data_dir / "usage_stats.db"   # 使用 pathlib 的 / 运算符拼接路径
        self.recorder = StatsRecorder(self.db_path)

        self.recall_message_ids: Dict[str, List[str]] = {}
        self.active_sessions: Dict[str, Any] = {}

        # 3. 构建指令到处理器的映射
        self.cmd_map = {
            "表情列表": self.handle_meme_list,
            "表情详情": self.handle_meme_info,
            "表情详细": self.handle_meme_info,
            "表情搜索": self.handle_meme_search,
            "刷新表情": self.handle_refresh_memes,
            "禁用表情": self.handle_disable_meme,
            "启用表情": self.handle_enable_meme,
            "管理列表": self.handle_manager_list,
            "全局禁用表情": self.handle_global_disable_meme,
            "全局启用表情": self.handle_global_enable_meme,
            "群管理员": self.handle_group_admin_manager,
            "表情调用统计": self.handle_meme_stats,
            "随机表情": self.handle_random_meme,
            "水平翻转": "flip_horizontal",
            "竖直翻转": "flip_vertical",
            "旋转": "rotate",
            "缩放": "resize",
            "裁剪": "crop",
            "灰度": "grayscale",
            "反色": "invert",
            "水平拼接": "merge_horizontal",
            "竖直拼接": "merge_vertical",
            "gif分解": "gif_split",
            "gif合成": "gif_merge",
            "gif倒放": "gif_reverse",
            "gif变速": "gif_change_duration"
        }

        # 4. 启动后台任务
        asyncio.create_task(self.meme_manager.refresh_memes(self.api_client))
        
        PermissionManager.get_instance(
            superusers=self.superusers,
            perms=self.config.get("perms", {}),
            recorder_instance=self.recorder
        )
        logger.info("权限系统在插件初始化时加载完成。")
        self.processing_events = set()

    @filter.event_message_type(EventMessageType.ALL, priority=100)
    async def universal_handler(self, event: AstrMessageEvent):
        if str(event.get_sender_id()) == str(event.get_self_id()): return

        session_id = UserInGroupSessionFilter().filter(event)
        if session_id in self.active_sessions:
            session_future = self.active_sessions[session_id].get("future")
            if session_future and not session_future.done():
                session_future.set_result(event)
                event.stop_event()
                return

        event_key = None
        try:
            event_key = (event.get_session_id(), event.message_obj.message_id)
            if event_key in self.processing_events: return
            self.processing_events.add(event_key)
        except Exception: return

        try:
            message_text = event.get_message_str().strip()
            if not message_text.startswith(self.prefix): return
            
            cleaned_text = message_text[len(self.prefix):].strip()
            if not cleaned_text: return

            if "表情统计" in cleaned_text:
                async for r in self.handle_meme_stats(event, cleaned_text): yield r
                return
            
            sorted_cmds = sorted(self.cmd_map.keys(), key=len, reverse=True)
            for cmd in sorted_cmds:
                if cmd == "表情统计": continue
                if cleaned_text.startswith(cmd):
                    arg_text = cleaned_text[len(cmd):].strip()
                    handler_or_op = self.cmd_map[cmd]

                    if isinstance(handler_or_op, str):
                        # 图片工具，是生成器，需要 async for
                        async for r in self.handle_image_tool(event, handler_or_op, arg_text): yield r
                    else:
                        # 【核心修正】检查处理器是“生成器”还是“协程”
                        if inspect.isasyncgenfunction(handler_or_op):
                            # 如果是生成器 (如 handle_meme_search), 则使用 async for
                            async for r in handler_or_op(event, arg_text):
                                yield r
                        else:
                            # 如果是协程 (如 meme_generate_handler), 则创建后台任务
                            asyncio.create_task(handler_or_op(event, arg_text))
                    return

            for sc_data in self.meme_manager.shortcuts:
                if await self.recorder.is_meme_disabled(sc_data["meme"].key, event.get_group_id()): continue
                if match := sc_data["pattern"].fullmatch(cleaned_text):
                    asyncio.create_task(self.handle_shortcut(event, sc_data["meme"], sc_data["shortcut"], match, ""))
                    return
                if match := sc_data["pattern"].match(cleaned_text):
                    remaining_text = cleaned_text[match.end():].strip()
                    asyncio.create_task(self.handle_shortcut(event, sc_data["meme"], sc_data["shortcut"], match, remaining_text))
                    return
            
            if keyword := self.meme_manager.find_keyword_in_text(cleaned_text, self.fuzzy_match):
                if meme_info := self.meme_manager.find_meme_by_keyword(keyword):
                    if not await self.recorder.is_meme_disabled(meme_info.key, event.get_group_id()):
                        # 【核心修正】确保此处也使用 asyncio.create_task
                        asyncio.create_task(self.meme_generate_handler(event, meme_info, cleaned_text))
                        
        finally:
            # 在 finally 块中，先检查 event_key 是否已被成功赋值
            if event_key:
                self.processing_events.discard(event_key)

    async def terminate(self):
        """插件卸载/停用时调用，用于释放资源"""
        await self.api_client.close()
        await self.recorder.close()
        logger.info("MemeMakerApiPlugin 成功终止，所有连接已关闭。")