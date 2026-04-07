import re
import io
import time
import shlex
import base64
import random
import asyncio
import zipfile
import filetype
import tempfile
from typing import Dict, Any, List, Optional, Union, AsyncGenerator
from datetime import datetime

from argparse import ArgumentError
import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult
from astrbot.api import logger
from astrbot.core.utils.session_waiter import SessionFilter

from ..models import MemeInfo, MemeParams
from ..exceptions import ArgParseError, APIError, NoExitArgumentParser

# --- 【核心新增】定义用户隔离的会话过滤器 ---
class UserInGroupSessionFilter(SessionFilter):
    """
    一个自定义的会话过滤器，用于在群聊中隔离不同用户的交互会话。
    - 在群聊中，它使用 "群号-用户ID" 作为唯一标识。
    - 在私聊中，它使用 "用户ID" 作为唯一标识。
    """
    def filter(self, event: AstrMessageEvent) -> str:
        if group_id := event.get_group_id():
            return f"{group_id}-{event.get_sender_id()}"
        return event.get_sender_id()


class GenerationHandlers:
    """一个 Mixin 类，包含所有表情包生成的核心逻辑"""

    # --- 【核心重构】新的、统一的发送逻辑准备函数 ---
    async def _prepare_send_results(self, event: AstrMessageEvent, result_obj: Union[bytes, List[bytes]]) -> AsyncGenerator[MessageEventResult, None]:
        """
        一个私有的异步生成器，用于准备所有要发送的消息结果。
        它包含了所有复杂的发送策略判断，但只 yield 结果，不关心最终如何发送。
        """
        if not result_obj:
            yield event.plain_result("图片处理失败，未收到结果。")
            return
        
        image_list = [result_obj] if isinstance(result_obj, bytes) else result_obj
        if not image_list:
            yield event.plain_result("图片处理失败，未收到结果。")
            return

        if len(image_list) <= self.direct_send_threshold:
            yield event.chain_result([Comp.Image.fromBytes(img_bytes) for img_bytes in image_list])
            return

        elif self.send_as_zip_enabled and len(image_list) > self.zip_threshold:
            yield event.plain_result(f"图片过多（{len(image_list)}张），将打包为 .zip 文件发送...")
            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, img_bytes in enumerate(image_list):
                    ext = filetype.guess_extension(img_bytes) or "png"
                    zf.writestr(f"image_{i+1}.{ext}", img_bytes)
            zip_buffer.seek(0)
            
            if event.get_platform_name() == "aiocqhttp" and hasattr(event, "bot") and event.get_group_id():
                try:
                    filename = f"meme_images_{int(time.time())}.zip"
                    if self.zip_use_base64:
                        zip_bytes = zip_buffer.getvalue()
                        base64_str = base64.b64encode(zip_bytes).decode()
                        file_payload = f"base64://{base64_str}"
                        await event.bot.upload_group_file(group_id=int(event.get_group_id()), file=file_payload, name=filename)
                    else:
                        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                            tmp.write(zip_buffer.getvalue())
                            tmp_path = tmp.name
                        try:
                            await event.bot.upload_group_file(group_id=int(event.get_group_id()), file=tmp_path, name=filename)
                        finally:
                            os.remove(tmp_path)
                except Exception as e:
                    logger.error(f"发送zip文件失败: {e}", exc_info=True)
                    yield event.plain_result("发送zip文件失败，请检查后台日志。")
            else:
                yield event.plain_result("当前平台或私聊不支持发送文件。")
            return

        elif self.send_forward_msg:
            yield event.plain_result(f"处理完成，生成 {len(image_list)} 张图片，将以合并转发形式发送：")
            if event.get_platform_name() == "aiocqhttp" and hasattr(event, "bot"):
                bot_id = event.get_self_id()
                bot_name = self.bot_name
                messages = [{"type": "node", "data": {"name": bot_name, "uin": bot_id, "content": [{"type": "image", "data": {"file": f"base64://{base64.b64encode(img_bytes).decode()}"}}]}} for img_bytes in image_list]
                try:
                    if group_id := event.get_group_id():
                        await event.bot.send_group_forward_msg(group_id=int(group_id), messages=messages)
                    else:
                        yield event.plain_result("私聊不支持发送合并转发，将逐条发送...")
                        for img_bytes in image_list:
                            yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
                            await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"发送合并转发消息失败: {e}", exc_info=True)
                    yield event.plain_result("发送合并转发消息失败，请检查后台日志。")
            else:
                yield event.plain_result("当前平台不支持发送合并转发，将逐条发送...")
                for img_bytes in image_list:
                    yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
                    await asyncio.sleep(0.5)
            return
        
        else:
            yield event.plain_result(f"处理完成，共生成 {len(image_list)} 张图片：")
            for img_bytes in image_list:
                yield event.chain_result([Comp.Image.fromBytes(img_bytes)])
                await asyncio.sleep(0.5)


    async def _send_and_record(self, event: AstrMessageEvent, text: str):
        """ (已改造) 主动发送文本提示，并根据配置决定是否记录其ID """
        session_id = UserInGroupSessionFilter().filter(event)
        try:
            # 使用 self.context.send_message 主动发送消息
            # 注意：此方法无法直接返回 message_id，撤回功能依赖 event.bot
            if self.recall_enabled and event.get_platform_name() == "aiocqhttp" and hasattr(event, "bot"):
                sent_msg = None
                if group_id := event.get_group_id():
                    sent_msg = await event.bot.send_group_msg(group_id=int(group_id), message=text)
                else:
                    sent_msg = await event.bot.send_private_msg(user_id=int(event.get_sender_id()), message=text)
                
                if sent_msg and sent_msg.get("message_id"):
                    if session_id not in self.recall_message_ids:
                        self.recall_message_ids[session_id] = []
                    self.recall_message_ids[session_id].append(str(sent_msg["message_id"]))
                    logger.info(f"成功记录待撤回消息ID: {sent_msg['message_id']} for session: {session_id}")
            else:
                # 如果不启用撤回或平台不支持，则使用更通用的发送方式
                await self.context.send_message(event.unified_msg_origin, MessageChain([Comp.Plain(text)]))
        except Exception as e:
            logger.error(f"_send_and_record 失败: {e}", exc_info=True)

    async def _cleanup_prompts(self, event: AstrMessageEvent):
        """辅助函数2：清理当前会话中已记录的所有提示消息"""
        if not self.recall_enabled:
            return
        
        # 【核心修正】使用与 _send_and_record 完全相同的过滤器来生成 session_id
        session_id = UserInGroupSessionFilter().filter(event)
        
        if session_id in self.recall_message_ids:
            ids_to_recall = self.recall_message_ids.pop(session_id, [])
            if not ids_to_recall: return
            
            logger.info(f"检测到会话结束，准备撤回 {len(ids_to_recall)} 条消息...")
            for msg_id in ids_to_recall:
                asyncio.create_task(self._recall_single_msg(event, msg_id))

    async def _recall_single_msg(self, event: AstrMessageEvent, msg_id: str):
        """辅助函数3：具体执行单条消息的撤回操作"""
        if not (event.get_platform_name() == "aiocqhttp" and hasattr(event, "bot")):
            return
        await asyncio.sleep(0.5)
        try:
            await event.bot.delete_msg(message_id=int(msg_id))
            logger.info(f"成功发送撤回指令 for msg_id {msg_id}")
        except Exception as e:
            logger.warning(f"撤回消息 {msg_id} 失败: {e}")

    async def _send_results_actively(self, event: AstrMessageEvent, result_obj: Union[bytes, List[bytes]]):
        """ (已简化) 主动发送器，用于后台工人 """
        async for res in self._prepare_send_results(event, result_obj):
            # 将 MessageEventResult 对象转换为 MessageChain 并主动发送
            await self.context.send_message(event.unified_msg_origin, MessageChain(res.chain))

    async def _session_worker(self, event: AstrMessageEvent, session_id: str, meme_info: MemeInfo):
        """ 
        独立的后台会话工人。
        它被创建后，会接管所有长时间运行的任务，包括：
        1. 判断是否需要交互
        2. 在需要时，循环等待用户输入
        3. 参数集齐后，调用API制作表情
        4. 发送最终结果
        5. 在任务结束时（成功、失败或超时），清理会话状态和提示消息
        """
        try:
            # 从"状态中心"获取自己的专属状态
            session_state = self.active_sessions.get(session_id)
            if not session_state: 
                logger.warning(f"后台工人启动，但未找到会话 {session_id} 的状态。")
                return
            
            p = session_state["params"]

            # 这是一个嵌套函数，只负责最后的"制作-发送"步骤
            async def _final_generate_and_send():
                try:
                    # 再次获取最新状态，以防万一
                    state = self.active_sessions.get(session_id, {})
                    final_texts, final_images = state.get("texts", []), state.get("images", [])
                    final_texts = final_texts[:p.max_texts]
                    final_images = final_images[:p.max_images]

                    tasks = [self.api_client.upload_image(b) for b in final_images]
                    image_ids = await asyncio.gather(*tasks)
                    image_payload = [{"id": img_id, "name": f"img{i}"} for i, img_id in enumerate(image_ids)]
                    final_payload = {"texts": final_texts, "images": image_payload, "options": state.get("options", {})}
                    
                    # 更新状态为"正在制作中"，实现状态锁
                    self.active_sessions[session_id]["status"] = "generating"
                    
                    image_data = await self.api_client.generate_meme(meme_info.key, final_payload)
                    await self.recorder.record_usage(meme_info.key, event.get_sender_id(), event.get_group_id())
                    await self._send_results_actively(event, image_data)
                except Exception as e:
                    logger.error(f"最终生成步骤出错: {e}", exc_info=True)
                    await self._send_and_record(event, "制作表情的最后一步失败了，呜呜...")

            # --- 交互式等待的主循环 ---
            if not (len(session_state["texts"]) >= p.min_texts and len(session_state["images"]) >= p.min_images):
                
                # 如果交互功能被禁用，则直接报错并退出
                if not self.interactive_enabled:
                    prompts = []
                    if len(session_state["texts"]) < p.min_texts: prompts.append(f"需要 {p.min_texts - len(session_state['texts'])} 段文字")
                    if len(session_state["images"]) < p.min_images: prompts.append(f"需要 {p.min_images - len(session_state['images'])} 张图片")
                    await self._send_and_record(event, f"参数不足：{'、'.join(prompts)}。（提示：可在后台配置中开启交互功能）")
                    return

                # 发送初始提示
                prompts = []
                if len(session_state["texts"]) < p.min_texts: prompts.append(f"需要 {p.min_texts - len(session_state['texts'])} 段文字")
                if len(session_state["images"]) < p.min_images: prompts.append(f"需要 {p.min_images - len(session_state['images'])} 张图片")
                prompt_text = f"参数不足，请继续发送{'、'.join(prompts)}。{self.session_timeout}秒内无操作将自动取消。"
                cancel_hint = f"\n（可发送"{self.prefix}取消"来随时终止）"
                await self._send_and_record(event, prompt_text + cancel_hint)

                # 进入循环等待状态
                while not (len(session_state["texts"]) >= p.min_texts and len(session_state["images"]) >= p.min_images):
                    future = asyncio.Future()
                    self.active_sessions[session_id]["future"] = future
                    try:
                        next_event = await asyncio.wait_for(future, timeout=self.session_timeout)
                    except asyncio.TimeoutError:
                        await self.context.send_message(event.unified_msg_origin, MessageChain([Comp.Plain("输入超时或交互时间过长，制作已取消")]))
                        return

                    if next_event.get_message_str().strip() == f"{self.prefix}取消":
                        await self.context.send_message(next_event.unified_msg_origin, MessageChain([Comp.Plain("操作已取消。")]))
                        return

                    # 智能重提示和数据收集逻辑
                    needs_text = len(session_state["texts"]) < p.min_texts
                    needs_image = len(session_state["images"]) < p.min_images
                    provided_text = next_event.get_message_str().strip()
                    provided_images = await self._get_images_from_message(next_event)
                    is_valid_and_needed_input = (needs_text and provided_text) or (needs_image and provided_images)

                    if is_valid_and_needed_input:
                        session_state["invalid_input_count"] = 0
                        if needs_text and provided_text: session_state["texts"].extend(provided_text.split())
                        if needs_image and provided_images: session_state["images"].extend(provided_images)
                        if len(session_state["texts"]) >= p.min_texts and len(session_state["images"]) >= p.min_images:
                            await self._send_and_record(next_event, "参数已集齐，开始制作...")
                            break
                        else:
                            prompts = []
                            if len(session_state["texts"]) < p.min_texts: prompts.append(f"还差 {p.min_texts - len(session_state['texts'])} 段文字")
                            if len(session_state["images"]) < p.min_images: prompts.append(f"还差 {p.min_images - len(session_state['images'])} 张图片")
                            await self._send_and_record(next_event, f"{'、'.join(prompts)}。")
                    else:
                        session_state["invalid_input_count"] += 1
                        if self.reprompt_enabled and session_state["invalid_input_count"] >= self.reprompt_threshold:
                            smart_prompt = ""
                            if not needs_text and provided_text: smart_prompt = "文字已经够啦，请发送我需要的图片哦~"
                            elif not needs_image and provided_images: smart_prompt = "图片已经够啦，我现在需要的是文字~"
                            if smart_prompt:
                                await self._send_and_record(next_event, smart_prompt)
                                session_state["invalid_input_count"] = 0
            
            # 当循环结束 (或一开始就满足条件时)，执行最终制作
            await _final_generate_and_send()
        
        except Exception as e:
            logger.error(f"会话工人任务 '{meme_info.key}' 失败: {e}", exc_info=True)
            await self._send_and_record(event, "表情制作失败了，呜呜...")
        finally:
            # 任务结束，无论成功失败，都清理会话和提示
            await self._cleanup_prompts(event)
            self.active_sessions.pop(session_id, None)
            logger.debug(f"后台工人任务结束，会话 {session_id} 已清理。")

    async def handle_shortcut(self, event: AstrMessageEvent, meme: MemeInfo, shortcut: Dict, match: re.Match, remaining_text: str = ""):
        try:
            logger.debug(f"快捷指令匹配成功: {meme.key}")
            match_dict = match.groupdict()
            texts = [t.format(**match_dict) for t in shortcut.get("texts", [])]
            options = {k: v.format(**match_dict) if isinstance(v, str) else v for k, v in shortcut.get("options", {}).items()}
            names = [n.format(**match_dict) for n in shortcut.get("names", [])]
            event.set_extra("shortcut_names", names)

            await self.meme_generate_handler(event, meme, remaining_text, initial_options=options, initial_texts=texts)

        except Exception as e:
            logger.error(f"处理快捷指令失败: {e}", exc_info=True)
        finally:
            # 在后台任务中，不再需要手动停止事件
            event.clear_extra()

    async def meme_generate_handler(self, event: AstrMessageEvent, meme_info: MemeInfo, text: str, initial_options: Dict = {}, initial_texts: List[str] = []):
        """
        现在只作为一个快速响应的"启动器"。
        它的职责是：检查状态锁 -> 创建会话状态 -> 启动后台工人 -> 立刻返回。
        """
        session_id = UserInGroupSessionFilter().filter(event)

        if session_id in self.active_sessions:
            # 状态锁检查
            await self._send_and_record(event, "您上一个表情正在制作中，请稍等片刻~")
            return

        try:
            # 初始化会话状态
            shortcut_texts = initial_texts
            shortcut_options = initial_options
            parsed_texts, initial_images, parsed_options = await self.build_meme_payload(event, meme_info, text)
            final_texts = shortcut_texts + parsed_texts
            final_options = shortcut_options
            final_options.update(parsed_options)
            p = meme_info.params
            if len(final_texts) == 0 and p.default_texts:
                final_texts = p.default_texts

            session_state = {
                "texts": final_texts, "images": initial_images, "options": final_options,
                "params": p, "invalid_input_count": 0, "status": "waiting_for_input"
            }
            self.active_sessions[session_id] = session_state
            
            # 创建并启动独立的后台工人任务，然后本函数就结束了
            asyncio.create_task(self._session_worker(event, session_id, meme_info))
        
        except Exception as e:
            logger.error(f"启动会话 '{meme_info.key}' 失败: {e}", exc_info=True)
            # 如果启动失败，也需要清理可能已创建的会话
            self.active_sessions.pop(session_id, None)
            await self._send_and_record(event, "开启表情制作任务失败了...")

    # --- 以下是其他辅助函数，保持不变 ---

    async def _get_images_from_message(self, event: AstrMessageEvent) -> List[bytes]:
        image_bytes_list: List[bytes] = []
        async def _process(seg):
            if isinstance(seg, Comp.Image):
                img_bytes: Optional[bytes] = None
                if hasattr(seg, "file") and seg.file:
                    content = seg.file
                    if isinstance(content, str) and content.startswith("base64://"): img_bytes = base64.b64decode(content[len("base64://"):])
                    elif isinstance(content, bytes): img_bytes = content
                if not img_bytes and hasattr(seg, "url") and seg.url: img_bytes = await self.api_client._download_image(seg.url)
                if img_bytes: image_bytes_list.append(img_bytes)
            elif isinstance(seg, Comp.At) and seg.qq:
                if b := await self._get_avatar(str(seg.qq)): image_bytes_list.append(b)
        msgs = event.get_messages()
        if reply := next((s for s in msgs if isinstance(s, Comp.Reply)), None):
            if getattr(reply, 'chain', None):
                for s in reply.chain: await _process(s)
        for s in msgs: await _process(s)
        return image_bytes_list

    async def build_meme_payload(self, event: AstrMessageEvent, meme_info: MemeInfo, text: str) -> (List[str], List[bytes], Dict):
        image_bytes_list: List[bytes] = []
        shortcut_names = event.get_extra("shortcut_names") or []
        
        initial_images = await self._get_images_from_message(event)
        image_bytes_list.extend(initial_images)
        
        for name in shortcut_names:
            if name.isdigit():
                if b := await self._get_avatar(name):
                    image_bytes_list.append(b)
        
        if self.use_sender_when_no_image and len(image_bytes_list) < meme_info.params.min_images:
            if b := await self._get_avatar(event.get_sender_id()):
                image_bytes_list.insert(0, b)
        
        text_to_parse = text.strip()
        
        keyword_in_text = self.meme_manager.find_keyword_in_text(text_to_parse, self.fuzzy_match)
        if keyword_in_text:
            text_to_parse = text_to_parse.replace(keyword_in_text, "", 1).strip()
        
        try:
            args = shlex.split(text_to_parse)
        except ValueError:
            args = text_to_parse.split()
        
        parser = NoExitArgumentParser(prog=f"{self.prefix}{meme_info.key}", add_help=False)
        type_mapping = {"integer": int, "float": float, "string": str}
        for opt in meme_info.params.options:
            flags, pf = [], opt.parser_flags
            if pf.get("long", True):
                flags.append(f"--{opt.name}")
            if pf.get("short", False) and opt.name:
                flags.append(f"-{opt.name[0]}")
            for alias in pf.get("long_aliases", []):
                flags.append(f"--{alias}")
            for alias in pf.get("short_aliases", []):
                flags.append(f"--{alias}")
                if len(alias) == 1:
                    flags.append(f"-{alias}")
            if not (unique_flags := list(dict.fromkeys(flags))):
                continue
            if opt.type == "boolean":
                parser.add_argument(*unique_flags, action="store_true", default=opt.default)
            else:
                parser.add_argument(*unique_flags, type=type_mapping.get(opt.type, str), default=opt.default)
        try:
            parsed_args, unknown_args = parser.parse_known_args(args)
            options_payload = {k: v for k, v in vars(parsed_args).items() if v is not None}
            texts = unknown_args
        except (ArgumentError, ValueError, ArgParseError) as e:
            raise ArgParseError(f"参数解析或类型转换错误: {e}")
        
        return texts, image_bytes_list, options_payload
    
    async def _get_avatar(self, user_id: str) -> Optional[bytes]:
        if not user_id.isdigit():
            return None
        return await self.api_client._download_image(f"http://q4.qlogo.cn/g?b=qq&nk={user_id}&s=640")
        
    async def _send_results(self, event: AstrMessageEvent, result_obj: Union[bytes, List[bytes]]):
        """ (已简化) yield-based 发送器，用于图片工具 """
        async for res in self._prepare_send_results(event, result_obj):
            yield res
    
    async def handle_random_meme(self, event: AstrMessageEvent, arg_text: str):
        try:
            temp_meme_info = MemeInfo(key="", params=MemeParams(min_images=0, max_images=99, min_texts=0, max_texts=99), date_created=datetime.now(), keywords=[])
            initial_texts, initial_images, _ = await self.build_meme_payload(event, temp_meme_info, arg_text)
            n_images_initial, n_texts_initial = len(initial_images), len(initial_texts)
            final_arg_text = arg_text
            n_images_filter, n_texts_filter = n_images_initial, n_texts_initial

            if n_images_initial == 0 and n_texts_initial == 0:
                logger.info("检测到无参数随机表情，启用默认文字模式")
                n_texts_filter = 1
                final_arg_text = "请输入文本"
            
            await self._send_and_record(event, "正在寻找合适的表情...")

            # 【核心修正】将列表推导式改为异步 for 循环
            available_memes = []
            for info in self.meme_manager.meme_infos.values():
                if not await self.recorder.is_meme_disabled(info.key, event.get_group_id()):
                    if (info.params.min_images <= n_images_filter <= info.params.max_images and
                        info.params.min_texts <= n_texts_filter <= info.params.max_texts):
                        available_memes.append(info)
            
            if not available_memes:
                await self._send_and_record(event, "找不到能制作这个素材的表情...换个试试？")
                return

            chosen_meme = random.choice(available_memes)
            await self.meme_generate_handler(event, chosen_meme, final_arg_text)

        except (ArgParseError, APIError, TimeoutError) as e:
            await self._send_and_record(event, f"出错了：{e}")
        except Exception as e:
            logger.error(f"随机表情失败: {e}", exc_info=True)
            await self._send_and_record(event, "随机表情失败了...")