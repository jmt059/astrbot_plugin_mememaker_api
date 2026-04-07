from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.core.utils.session_waiter import session_waiter, SessionController

# 依赖我们自己创建的模块
from ..models import MemeOption

class InfoHandlers:
    """
    一个 Mixin 类，包含所有信息查询相关的指令处理器。
    注意：此类中的方法会通过继承，在主插件类中使用，
    因此它可以访问 self.api_client, self.meme_manager 等主类属性。
    """
    def _format_meme_option(self, option: MemeOption) -> str:
        """辅助函数：格式化单个选项的详细信息"""
        flags, pf = [], option.parser_flags
        if pf.get("long", True): flags.append(f"--{option.name}")
        if pf.get("short", False): flags.append(f"-{option.name[0]}")
        flags.extend([f"--{a}" for a in pf.get("long_aliases", [])])
        flags.extend([f"-{a}" for a in pf.get("short_aliases", [])])
        
        text = f"  {'/'.join(flags)}"
        if option.type != "boolean":
            text += f" <{option.type.upper()}>"
        
        text += f"\n    说明: {option.description or '无'}"
        
        additions = []
        if option.type in ["integer", "float"]:
            if option.minimum is not None:
                additions.append(f"最小: {option.minimum}")
            if option.maximum is not None:
                additions.append(f"最大: {option.maximum}")
        if option.choices:
            additions.append(f"可选: {', '.join(str(c) for c in option.choices)}")
        if option.default is not None:
            additions.append(f"默认: {option.default}")
            
        if additions:
            text += f" ({' | '.join(additions)})"
        return text

    async def handle_meme_info(self, event: AstrMessageEvent, keyword: str):
        try:
            if not keyword:
                yield event.plain_result("请提供关键词，如：-表情详情 摸")
                return

            meme_info = self.meme_manager.find_meme_by_keyword(keyword)
            if not meme_info:
                yield event.plain_result(f"未找到“{keyword}”相关表情。")
                return

            # --- 这部分构建 info_text 的逻辑保持不变 ---
            p = meme_info.params
            info_text = f"表情名：{meme_info.key}"
            info_text += f"\n关键词：{', '.join(meme_info.keywords)}"
            if meme_info.shortcuts:
                shortcuts = ", ".join([sc.get("humanized") or sc.get("pattern", "") for sc in meme_info.shortcuts])
                info_text += f"\n快捷指令：{shortcuts}"
            if meme_info.tags:
                info_text += f"\n标签：{', '.join(meme_info.tags)}"
            info_text += f"\n需要图片数：{p.min_images}" + (f" ~ {p.max_images}" if p.min_images != p.max_images else "")
            info_text += f"\n需要文字数：{p.min_texts}" + (f" ~ {p.max_texts}" if p.min_texts != p.max_texts else "")
            if p.default_texts:
                info_text += f"\n默认文字：{', '.join(p.default_texts)}"
            if p.options:
                options_info = "\n".join([self._format_meme_option(opt) for opt in p.options])
                info_text += f"\n\n--- 可选选项 ---\n{options_info}"
            # --- 逻辑结束 ---

            # 获取预览图
            preview_img = await self.api_client.get_meme_preview(meme_info.key)

            # --- 【核心修改】 ---
            # 1. 构建包含详情文字和预览图的消息链
            message_chain = [
                Comp.Plain(info_text + "\n\n--- 表情预览 ---"),
                Comp.Image.fromBytes(preview_img)
            ]

            # 2. 一次性发送完整的图文消息
            yield event.chain_result(message_chain)

        except Exception as e:
            logger.error(f"获取表情详情失败: {e}", exc_info=True)
            yield event.plain_result("获取表情详情失败了，呜呜...")
        finally:
            event.stop_event()