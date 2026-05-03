import re
import asyncio
from typing import Dict, Any, List, Optional

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger

from ..exceptions import ArgParseError, APIError

class ToolHandlers:
    """一个 Mixin 类，包含所有图片工具相关的指令处理器和辅助函数"""

    async def handle_image_tool(self, event: AstrMessageEvent, operation: str, arg_text: str):
        try:
            op_config = {"merge_horizontal": 2, "merge_vertical": 2, "gif_merge": 2}
            min_images = op_config.get(operation, 1)
            image_ids = await self._get_images_for_tool(event, min_images=min_images)
            if not image_ids:
                return

            result_obj = None
            if operation == "resize":
                width, height = self._parse_resize_args(arg_text)
                result_obj = await self.api_client.resize(image_ids[0], width, height)
            elif operation == "crop":
                image_info = await self.api_client.inspect_image(image_ids[0])
                left, top, right, bottom = self._parse_crop_args(arg_text, image_info)
                result_obj = await self.api_client.crop(image_ids[0], left, top, right, bottom)
            elif operation == "gif_change_duration":
                image_info = await self.api_client.inspect_image(image_ids[0])
                duration = self._parse_gif_change_duration_args(arg_text, image_info)
                result_obj = await self.api_client.gif_change_duration(image_ids[0], duration)
            elif operation in ["flip_horizontal", "flip_vertical", "grayscale", "invert", "gif_reverse"]:
                result_obj = await getattr(self.api_client, operation)(image_ids[0])
            elif operation == "gif_split":
                result_obj = await self.api_client.gif_split(image_ids[0])
            elif operation in ["merge_horizontal", "merge_vertical"]:
                result_obj = await getattr(self.api_client, operation)(image_ids)
            elif operation == "rotate":
                degrees = float(arg_text or 90.0)
                result_obj = await self.api_client.rotate(image_ids[0], degrees)
            elif operation == "gif_merge":
                duration = float(arg_text or 0.1)
                result_obj = await self.api_client.gif_merge(image_ids, duration)
            
            async for r in self._send_results(event, result_obj):
                yield r

        except (APIError, ValueError, ArgParseError) as e:
            yield event.plain_result(f"操作失败: {e}")
        except Exception as e:
            logger.error(f"图片操作 {operation} 失败: {e}", exc_info=True)
            yield event.plain_result(f"图片操作失败: {e}")
        finally:
            event.stop_event()

    async def _get_images_for_tool(self, event: AstrMessageEvent, min_images: int = 1) -> List[str]:
        """从消息中提取所需数量的图片，上传并返回 image_id 列表"""
        image_data = await self._get_images_from_message(event)
        image_bytes_list = [item[0] for item in image_data]
        
        if len(image_bytes_list) < min_images:
            # 如果不够，自动补充发送者头像
            if self.use_sender_when_no_image:
                if b := await self._get_avatar(event.get_sender_id()):
                    image_bytes_list.insert(0, b)

        if len(image_bytes_list) < min_images:
            raise ArgParseError(f"图片数量不足，此操作需要 {min_images} 张图片。")
        
        images_to_upload = image_bytes_list[:min_images] if min_images > 0 else image_bytes_list
        tasks = [self.api_client.upload_image(img_bytes) for img_bytes in images_to_upload]
        return await asyncio.gather(*tasks)

    def _parse_resize_args(self, text: str) -> (Optional[int], Optional[int]):
        width, height = None, None
        if match := re.fullmatch(r"(\d{1,4})?[*xX, ](\d{1,4})?", text):
            w, h = match.groups()
            if w: width = int(w)
            if h: height = int(h)
            return width, height
        raise ArgParseError("缩放尺寸格式不正确，请使用如: 100x200, 100x, x200")
        
    def _parse_crop_args(self, text: str, image_info: Dict) -> (int, int, int, int):
        if match := re.fullmatch(r"(\d{1,4})[, ](\d{1,4})[, ](\d{1,4})[, ](\d{1,4})", text):
            return tuple(map(int, match.groups()))
        img_w, img_h = image_info["width"], image_info["height"]
        if match := re.fullmatch(r"(\d{1,4})[*xX, ](\d{1,4})", text):
            width, height = map(int, match.groups())
        elif match := re.fullmatch(r"(\d{1,2})[:：比](\d{1,2})", text):
            wp, hp = map(int, match.groups())
            size = min(img_w / wp, img_h / hp)
            width, height = int(wp * size), int(hp * size)
        else:
            raise ArgParseError("裁剪格式不正确，请使用如: 0,0,100,100 或 100x100 或 16:9")
        left = (img_w - width) // 2; top = (img_h - height) // 2
        return left, top, left + width, top + height
        
    def _parse_gif_change_duration_args(self, text: str, image_info: Dict) -> float:
        p_float = r"\d{0,3}\.?\d{1,3}"
        if match := re.fullmatch(rf"({p_float})fps", text, re.I):
            duration = 1 / float(match.group(1))
        elif match := re.fullmatch(rf"({p_float})(m?)s", text, re.I):
            duration = float(match.group(1)) / 1000 if match.group(2) else float(match.group(1))
        else:
            duration = image_info.get("average_duration") or 0.1
            if match := re.fullmatch(rf"({p_float})(?:x|X|倍速?)", text):
                duration /= float(match.group(1))
            elif match := re.fullmatch(rf"({p_float})%", text):
                duration /= float(match.group(1)) / 100
            else:
                raise ArgParseError("变速格式不正确，请使用如: 0.5x, 50%, 20fps, 0.05s")
        if duration < 0.02:
            raise ArgParseError(f"帧间隔必须大于 0.02s (50fps)，当前为 {duration:.3f}s")
        return duration