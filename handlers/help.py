# 文件：astrbot_plugin_meme_maker_api/handlers/help.py (性能优化版)

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict

from astrbot.api.event import AstrMessageEvent
import astrbot.api.message_components as Comp
from astrbot.api import logger

class HelpHandlers:
    """一个 Mixin 类，包含表情帮助和表情列表相关的指令处理器"""

    async def handle_meme_list(self, event: AstrMessageEvent, _=None):
        try:
            logger.info("开始生成动态表情包列表图...")
            yield event.plain_result("正在生成动态列表，请稍候...")

            # --- 【核心优化】 ---
            # 1. 计算热门表情
            start_time = datetime.now(timezone.utc) - timedelta(days=self.label_hot_days)
            recent_meme_keys = await self.recorder.get_recent_meme_keys(start_time)
            # 1a. 预先计算所有关键词的出现次数
            hot_counts = Counter(recent_meme_keys)
            
            meme_properties: Dict[str, Dict[str, bool]] = {}
            now_utc = datetime.now(timezone.utc)
            new_timedelta = timedelta(days=self.label_new_days)

            for meme in self.meme_manager.meme_infos.values():
                try:
                    is_new = (now_utc - meme.date_created) < new_timedelta
                except (ValueError, TypeError):
                    is_new = False
                
                # 1b. 使用 O(1) 复杂度的字典查找，代替低效的 list.count()
                is_hot = hot_counts.get(meme.key, 0) >= self.label_hot_threshold
                
                is_disabled = await self.recorder.is_meme_disabled(meme.key, event.get_group_id())
                properties = {"new": is_new, "hot": is_hot, "disabled": is_disabled}
                meme_properties[meme.key] = properties
            
            image_data = await self.api_client.render_list_image(meme_properties)
            # --- 优化结束 ---

            # 构建并发送复合消息
            A_text = "触发：“-关键词 [文] [@人] [--选项]”\n"
            B_text = "-表情详情 <关键词> | -表情搜索 <关键词>\n"
            full_text = A_text + B_text
            
            message_chain = [
                Comp.Plain(full_text),
                Comp.Image.fromBytes(image_data)
            ]

            yield event.chain_result(message_chain)

        except Exception as e:
            logger.error(f"生成动态表情列表图失败: {e}", exc_info=True)
            yield event.plain_result("生成列表图失败了，呜呜...")
        finally:
            event.stop_event()

    async def handle_meme_help(self, event: AstrMessageEvent, _=None):
        p = self.prefix
        help_text = (
            f"【基础指令】\n"
            f"{p}表情列表: 查看所有支持表情\n"
            f"{p}表情详情 <词>: 查询具体用法\n"
            f"{p}表情搜索 <词>: 靠关键词找表情\n"
            f"{p}<表情名> [图/文]: 制作表情\n"
            f"{p}随机表情: 随机生成一张\n"
            f"{p}表情调用统计: 查看使用榜单\n"
            f"\n【图片处理】(发送图片附带指令)\n"
            f"支持: {p}水平翻转, {p}竖直翻转, {p}旋转, {p}缩放, {p}裁剪, {p}灰度, {p}反色, {p}水平/竖直拼接\n"
            f"GIF处理: {p}gif分解, {p}gif合成, {p}gif倒放, {p}gif变速\n"
            f"\n【群组管理】\n"
            f"{p}管理列表: 查看本群已禁用列表\n"
            f"{p}禁用表情 <词>: 本群禁用该表情\n"
            f"{p}启用表情 <词>: 重新启用该表情\n"
            f"\n【全局管理】(超管可用)\n"
            f"{p}刷新表情: 重新加载配置数据\n"
            f"{p}全局禁用表情 <词>: 全局禁用\n"
            f"{p}全局启用表情 <词>: 全局启用"
        )
        yield event.plain_result(help_text)
        event.stop_event()
