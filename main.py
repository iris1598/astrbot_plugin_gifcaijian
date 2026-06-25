import re
import io
import os
import asyncio
import aiohttp
import tempfile
import urllib.parse
from PIL import Image as PILImage, ImageSequence, ImageFilter, ImageOps, ImageEnhance
from astrbot.api.event import filter
from astrbot.api.all import *
from astrbot.api import logger
import astrbot.api.message_components as Comp

# 尝试导入 imageio
try:
    import imageio
except ImportError:
    imageio = None


@register(
    "astrbot_plugin_gifcaijian",
    "shskjw",
    "支持GIF/APNG/WebP转换、裁剪、本地图片转线稿及多图合成(终极稳定版)",
    "1.4.2",
    "https://github.com/shkjw/astrbot_plugin_gifcaijian",
)
class SpriteToGifPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.cfg = config if config is not None else {}

        if imageio is None:
            logger.warning("插件[astrbot_plugin_gifcaijian]检测到缺少 imageio 库。请运行 pip install imageio[ffmpeg]")

    # --- 核心工具：统一保存动画 ---
    def _save_animation(self, output: io.BytesIO, frames: list, duration_ms: int, loop: int = 0):
        fmt = self.cfg.get('output_format', 'GIF').upper()
        if fmt == 'GIF':
            frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:], duration=duration_ms,
                           loop=loop, optimize=True, disposal=2)
        elif fmt == 'APNG':
            frames[0].save(output, format='PNG', save_all=True, append_images=frames[1:], duration=duration_ms,
                           loop=loop, optimize=True, default_image=True)
        elif fmt == 'WEBP':
            frames[0].save(output, format='WEBP', save_all=True, append_images=frames[1:], duration=duration_ms,
                           loop=loop, method=3, quality=80)
        else:
            frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:], duration=duration_ms,
                           loop=loop, optimize=True, disposal=2)

    # --- 辅助方法: 获取单张图片URL (增强版) ---
    def _get_image_url(self, event: AstrMessageEvent) -> str:
        """获取目标图片URL：优先回复的图片 -> 当前消息的图片 -> At对象的头像"""
        
        # 1. 检查回复链
        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.Reply) and seg.chain:
                    for item in seg.chain:
                        if isinstance(item, Comp.Image) and item.url: 
                            return item.url
                        if isinstance(item, dict) and item.get('type') == 'image':
                            return item.get('data', {}).get('url') or item.get('url') or item.get('file')

        # 2. 检查当前消息中的图片
        # 优先使用 AstrBot 提供的便捷方法
        if hasattr(event, "get_images"):
            images = event.get_images()
            if images: return images[0].url
            
        # 再次手动检查 chain (防止便捷方法遗漏)
        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.Image) and seg.url:
                    return seg.url
                if isinstance(seg, dict) and seg.get('type') == 'image':
                    return seg.get('data', {}).get('url') or seg.get('url') or seg.get('file')

        # 3. 检查 At (获取头像)
        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.At):
                    # 尝试排除机器人自己 (如果能获取到 self_id)
                    # 此处假设用户 At 别人是为了获取头像
                    user_id = str(seg.qq)
                    return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"

        return None

    # --- 新增: 递归提取所有图片 (支持合并转发、回复等) ---
    def _extract_images_from_chain(self, chain: list) -> list[str]:
        urls = []
        for item in chain:
            # 1. 直接是 Image 组件
            if isinstance(item, Comp.Image) and item.url:
                urls.append(item.url)
            # 2. 字典格式
            elif isinstance(item, dict):
                if item.get('type') == 'image':
                    url = item.get('data', {}).get('url') or item.get('url') or item.get('file')
                    if url and isinstance(url, str) and url.startswith('http'):
                        urls.append(url)
                # 3. 嵌套节点 (Forward Node)
                elif item.get('type') == 'node':
                    content = item.get('data', {}).get('content') or item.get('content')
                    if isinstance(content, list):
                        urls.extend(self._extract_images_from_chain(content))
            # 4. Reply 组件
            elif isinstance(item, Comp.Reply) and item.chain:
                urls.extend(self._extract_images_from_chain(item.chain))
            # 5. Nodes 组件
            elif isinstance(item, Comp.Nodes):
                if item.nodes:
                    for node in item.nodes:
                        if isinstance(node.content, list):
                            urls.extend(self._extract_images_from_chain(node.content))
        return urls

    async def _get_all_image_urls(self, event: AstrMessageEvent) -> list[str]:
        """获取上下文中所有的图片链接（包括当前消息、回复的消息、转发消息、At头像）"""
        urls = []

        # 1. 检查 event.message_obj.message
        if hasattr(event.message_obj, "message") and isinstance(event.message_obj.message, list):
            urls.extend(self._extract_images_from_chain(event.message_obj.message))

        # 2. 补充 get_images
        if hasattr(event, "get_images"):
            imgs = event.get_images()
            for img in imgs:
                if img.url and img.url not in urls:
                    urls.append(img.url)
        
        # 3. 补充 At 头像
        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.At):
                    uid = str(seg.qq)
                    url = f"https://q1.qlogo.cn/g?b=qq&nk={uid}&s=640"
                    if url not in urls:
                        urls.append(url)

        # 去重但保持顺序
        seen = set()
        unique_urls = []
        for u in urls:
            if u not in seen:
                unique_urls.append(u)
                seen.add(u)
        return unique_urls

    # --- 辅助方法: 智能获取视频源 ---
    def _get_video_source(self, event: AstrMessageEvent) -> str:
        candidates = []

        def extract_from_item(item):
            url = getattr(item, 'url', None)
            if not url and isinstance(item, dict):
                url = item.get('data', {}).get('url') or item.get('url')
            if url and isinstance(url, str) and url.startswith('http'):
                return 100, url
            path = getattr(item, 'path', None)
            if not path and isinstance(item, dict):
                path = item.get('data', {}).get('path') or item.get('path')
            if path and isinstance(path, str) and os.path.isabs(path) and os.path.exists(path):
                return 90, path
            file_info = getattr(item, 'file', None)
            if not file_info and isinstance(item, dict):
                file_info = item.get('data', {}).get('file') or item.get('file')
            if file_info and isinstance(file_info, str):
                return 50, file_info
            return 0, None

        items_to_check = []
        if hasattr(event, "get_videos"):
            videos = event.get_videos()
            if videos: items_to_check.extend(videos)

        if hasattr(event.message_obj, "message"):
            for seg in event.message_obj.message:
                if isinstance(seg, Comp.Reply) and seg.chain:
                    items_to_check.extend(seg.chain)
                elif isinstance(seg, (Comp.Video, dict)):
                    items_to_check.append(seg)
                elif isinstance(seg, dict) and seg.get('type') == 'video':
                    items_to_check.append(seg)

        for item in items_to_check:
            score, val = extract_from_item(item)
            if val: candidates.append((score, val))

        if not candidates: return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # --- 通过API解析文件ID ---
    async def _resolve_file_via_api(self, event: AstrMessageEvent, file_id: str) -> str:
        try:
            logger.info(f"尝试通过API解析文件ID: {file_id}")
            res = await event.bot.api.call_action("get_file", file_id=file_id)
            if not res or not isinstance(res, dict): return None
            url = res.get('url')
            if url and url.startswith('http'): return url
            path = res.get('file')
            if path and os.path.exists(path): return path
            return url or path
        except Exception as e:
            logger.warning(f"API解析文件失败: {e}")
            return None

    # --- 智能参数解析 ---
    def _parse_video_args(self, text: str):
        default_scale = self.cfg.get('default_scale', 0.3)
        default_fps = self.cfg.get('default_fps', 10)
        params = {
            'start': 0.0, 'end': None, 'fps': default_fps,
            'step': 1, 'scale': default_scale, 'force_step': False
        }
        time_range = re.search(r'(\d+(?:\.\d+)?)[sS]?\s*[-~]\s*(\d+(?:\.\d+)?)[sS]?', text)
        if time_range:
            params['start'] = float(time_range.group(1))
            params['end'] = float(time_range.group(2))
            text = text.replace(time_range.group(0), " ")
        else:
            start_match = re.search(r'(?:开始|start)\s*(\d+(?:\.\d+)?)', text)
            dur_match = re.search(r'(?:时长|len|time)\s*(\d+(?:\.\d+)?)', text)
            if start_match: params['start'] = float(start_match.group(1))
            if dur_match: params['end'] = params['start'] + float(dur_match.group(1))

        step_match = re.search(r'(\d+)\s*/\s*(\d+)', text)
        if step_match:
            n1 = int(step_match.group(1))
            n2 = int(step_match.group(2))
            step_val = max(n1, n2)
            if step_val > 0:
                params['step'] = step_val
                params['fps'] = None
                params['force_step'] = True
            text = text.replace(step_match.group(0), " ")
        else:
            fps_match = re.search(r'(?:fps|帧率)\s*(\d+)', text)
            if fps_match: params['fps'] = int(fps_match.group(1))

        scale_match = re.search(r'\b(0\.\d+|1\.0)\b', text)
        if scale_match: params['scale'] = float(scale_match.group(1))
        if params['scale'] < 0.1: params['scale'] = 0.1
        if params['scale'] > 1.0: params['scale'] = 1.0
        return params

    # --- 核心处理逻辑 ---
    def _process_gif_core(self, video_path: str, params: dict, max_colors: int = 256):
        try:
            reader = imageio.get_reader(video_path, format='FFMPEG')
            meta = reader.get_meta_data()
            video_duration = meta.get('duration', 100)
            src_fps = meta.get('fps', 30) or 30
            start_t = params['start']
            end_t = params['end'] if params['end'] is not None else video_duration
            max_dur_conf = self.cfg.get('max_gif_duration', 10.0)
            warn_msg = ""
            if (end_t - start_t) > max_dur_conf:
                end_t = start_t + max_dur_conf
                warn_msg = f"(限时{max_dur_conf}s)"
            end_t = min(end_t, video_duration)
            if start_t >= video_duration: return None, f"❌ 开始时间超限", 0

            step = 1
            target_fps = 0
            if params.get('force_step'):
                step = params['step']
                target_fps = src_fps / step
            elif params.get('fps'):
                target_fps = params['fps']
                if target_fps > src_fps: target_fps = src_fps
                step = max(1, int(src_fps / target_fps))
            else:
                step = 3
                target_fps = src_fps / step

            frames = []
            output_fmt = self.cfg.get('output_format', 'GIF').upper()
            for i, frame in enumerate(reader):
                current_time = i / src_fps
                if current_time < start_t: continue
                if current_time > end_t: break
                if i % step == 0:
                    pil_img = PILImage.fromarray(frame)
                    w, h = pil_img.size
                    new_w = int(w * params['scale'])
                    new_h = int(h * params['scale'])
                    pil_img = pil_img.resize((new_w, new_h), PILImage.Resampling.BILINEAR)
                    if output_fmt == 'GIF' and max_colors < 256:
                        pil_img = pil_img.quantize(colors=max_colors, method=1, dither=PILImage.Dither.FLOYDSTEINBERG)
                    frames.append(pil_img)
                if len(frames) > 400:
                    warn_msg += " [帧数截断]"
                    break
            reader.close()
            if not frames: return None, "❌ 无有效帧", 0
            output = io.BytesIO()
            duration_ms = int(1000 / target_fps) if target_fps > 0 else 100
            self._save_animation(output, frames, duration_ms, loop=0)
            output.seek(0)
            size_mb = output.getbuffer().nbytes / 1024 / 1024
            info = f"时间:{start_t}-{end_t:.1f}s {warn_msg}\n格式:{output_fmt} | FPS:{target_fps:.1f}\n缩放:{params['scale']} | 体积:{size_mb:.2f}MB"
            return output, info, size_mb
        except Exception as e:
            return None, f"内部错误: {repr(e)}", 0

    def _worker_video_to_gif_wrapper(self, video_path: str, params: dict):
        if imageio is None: return "❌ 缺少依赖库 imageio", None
        max_colors = self.cfg.get('gif_max_colors', 256)
        gif_io, msg, size_mb = self._process_gif_core(video_path, params, max_colors)
        if not gif_io: return msg, None
        output_fmt = self.cfg.get('output_format', 'GIF').upper()
        if size_mb > 10.0 and output_fmt == 'GIF':
            new_params = params.copy()
            new_msg_prefix = f"⚠️ 初次体积{size_mb:.1f}MB过大，自动压缩中...\n"
            new_colors = 128 if max_colors > 128 else 64
            new_params['scale'] = round(params['scale'] * 0.8, 2)
            if new_params['scale'] < 0.1: new_params['scale'] = 0.1
            retry_io, retry_msg, retry_size = self._process_gif_core(video_path, new_params, new_colors)
            if retry_io and retry_size < size_mb:
                return new_msg_prefix + retry_msg, retry_io
            else:
                return f"⚠️ 压缩失败({retry_size:.1f}MB)，原版:\n" + msg, gif_io
        return "✅ 转换成功\n" + msg, gif_io

    async def _download_content(self, url: str) -> bytes:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=60) as resp:
                    if resp.status != 200: return None
                    return await resp.read()
            except:
                return None

    def _worker_local_line_art(self, img_bytes: bytes) -> bytes:
        """本地线稿生成算法"""
        try:
            # 1. 打开图片
            img = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")

            # 2. 转换为灰度
            gray = img.convert("L")

            # 3. 边缘检测 (FIND_EDGES 效果类似素描)
            edges = gray.filter(ImageFilter.FIND_EDGES)

            # 4. 颜色反转 (黑底白线 -> 白底黑线)
            result = ImageOps.invert(edges)

            # 5. 增强对比度 (让线条更清晰)
            enhancer = ImageEnhance.Contrast(result)
            result = enhancer.enhance(3.0)  # 提高对比度

            # 6. 保存
            output = io.BytesIO()
            result.save(output, format='JPEG', quality=90)
            return output.getvalue()
        except Exception as e:
            return None

    # --- 修复增强版: 本地图片转线稿 (无需API) ---
    @filter.command("图片转线稿")
    async def img_to_line_art(self, event: AstrMessageEvent):
        img_url = self._get_image_url(event)
        if not img_url:
            yield event.plain_result("❌ 请发送图片或回复图片")
            return

        yield event.plain_result("⏳ 正在处理(本地模式)...")

        # 1. 下载图片 (Bot自己下载，避免API防盗链问题)
        img_bytes = await self._download_content(img_url)
        if not img_bytes:
            yield event.plain_result("❌ 图片下载失败 (Bot无法访问该图片链接)")
            return

        # 2. 本地算法处理
        result_bytes = await asyncio.to_thread(self._worker_local_line_art, img_bytes)

        if result_bytes:
            yield event.chain_result([
                Comp.Plain("✅ 转换成功"),
                Comp.Image.fromBytes(result_bytes)
            ])
        else:
            yield event.plain_result("❌ 转换处理失败 (图片格式错误?)")

    @filter.command("视频转gif")
    async def video_to_gif_cmd(self, event: AstrMessageEvent):
        if imageio is None:
            yield event.plain_result("❌ 无法使用此功能：服务器缺少 imageio 库。")
            return
        msg_text = event.message_str.replace("视频转gif", "")
        params = self._parse_video_args(msg_text)
        raw_source = self._get_video_source(event)
        if not raw_source:
            yield event.plain_result("❌ 请回复一个视频或发送视频链接。")
            return
        valid_source = None
        if raw_source.startswith("http") or os.path.exists(raw_source):
            valid_source = raw_source
        else:
            yield event.plain_result("⏳ 正在请求视频地址...")
            valid_source = await self._resolve_file_via_api(event, raw_source)
            if not valid_source:
                yield event.plain_result(f"❌ 无法解析视频地址: {raw_source}")
                return
        fmt = self.cfg.get('output_format', 'GIF')
        time_info = f"{params['start']}s-" + (f"{params['end']}s" if params['end'] else "末尾")
        yield event.plain_result(f"⏳ 任务已接收 ({fmt})\n区间: {time_info}\n缩放: {params['scale']}")
        tmp_path = ""
        is_temp_file = False
        try:
            if valid_source.startswith("http"):
                max_size = self.cfg.get('max_video_size_mb', 50.0) * 1024 * 1024
                with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp_file:
                    tmp_path = tmp_file.name
                    is_temp_file = True
                headers = {"User-Agent": "Mozilla/5.0"}
                async with aiohttp.ClientSession() as session:
                    async with session.get(valid_source, headers=headers, timeout=120) as resp:
                        if resp.status != 200:
                            yield event.plain_result(f"❌ 下载失败 HTTP {resp.status}")
                            if os.path.exists(tmp_path): os.remove(tmp_path)
                            return
                        content_len = resp.headers.get('Content-Length')
                        if content_len and int(content_len) > max_size:
                            yield event.plain_result(f"❌ 视频超过大小限制")
                            if os.path.exists(tmp_path): os.remove(tmp_path)
                            return
                        with open(tmp_path, 'wb') as f:
                            f.write(await resp.read())
            else:
                tmp_path = valid_source
                is_temp_file = False
            result_msg, gif_bytes = await asyncio.to_thread(self._worker_video_to_gif_wrapper, tmp_path, params)
            if is_temp_file and os.path.exists(tmp_path): os.remove(tmp_path)
            if gif_bytes:
                yield event.chain_result([Comp.Plain(result_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
            else:
                yield event.plain_result(result_msg)
        except Exception as e:
            if is_temp_file and tmp_path and os.path.exists(tmp_path): os.remove(tmp_path)
            yield event.plain_result(f"❌ 处理异常: {repr(e)}")

    # --- 其他功能保持 ---
    def _parse_margins(self, text: str):
        margins = {'top': 0, 'bottom': 0, 'left': 0, 'right': 0}
        pattern = r'边距\s*([上下左右])?边?\s*(\d+)'
        matches = re.findall(pattern, text)
        for direction, amount_str in matches:
            try:
                amount = int(amount_str)
                if not direction:
                    for k in margins: margins[k] += amount
                elif direction == '上':
                    margins['top'] += amount
                elif direction == '下':
                    margins['bottom'] += amount
                elif direction == '左':
                    margins['left'] += amount
                elif direction == '右':
                    margins['right'] += amount
            except ValueError:
                pass
        clean_text = re.sub(pattern, " ", text)
        return clean_text, margins

    def _crop_image_data(self, img_data: bytes, margins: dict) -> tuple[bytes, str]:
        if all(v == 0 for v in margins.values()): return img_data, ""
        try:
            img = PILImage.open(io.BytesIO(img_data)).convert("RGBA")
            w, h = img.size
            l, u, r, d = margins['left'], margins['top'], w - margins['right'], h - margins['bottom']
            if l >= r or u >= d: return img_data, f"\n⚠️ 边距无效: {w}x{h} -> {l},{u},{r},{d}"
            output = io.BytesIO()
            img.crop((l, u, r, d)).save(output, format='PNG')
            return output.getvalue(), f"\n✂️ 已裁边距: 上{margins['top']} 下{margins['bottom']} 左{margins['left']} 右{margins['right']}"
        except Exception as e:
            return img_data, f"\n⚠️ 边距裁剪出错: {e}"

    async def _download_image(self, url: str) -> bytes:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, headers=headers, timeout=30) as resp:
                    if resp.status != 200: return None
                    return await resp.read()
            except:
                return None

    async def _handle_gif_task(self, event: AstrMessageEvent, algorithm_mode: int):
        msg_text = event.message_str
        clean_text, margins = self._parse_margins(msg_text)
        clean_text = clean_text.replace("合成1gif", "").replace("合成2gif", "").replace("合成gif", "")
        rows, cols, duration = 6, 6, 0.1
        grid_match = re.search(r'(\d+)\s*[*x×]\s*(\d+)', clean_text)
        if grid_match:
            rows, cols = int(grid_match.group(1)), int(grid_match.group(2))
            clean_text = clean_text.replace(grid_match.group(0), " ")
        dur_match = re.search(r'(\d+(?:\.\d+)?)', clean_text)
        if dur_match:
            try:
                val = float(dur_match.group(1))
                if 0 < val <= 60: duration = val
            except:
                pass
        img_url = self._get_image_url(event)
        if not img_url:
            yield event.plain_result("❌ 未检测到图片")
            return
        yield event.plain_result(f"⏳ 正在合成(算法{algorithm_mode})... ({rows}x{cols}, 每帧{duration}s)")
        img_data = await self._download_image(img_url)
        if not img_data:
            yield event.plain_result("❌ 图片下载失败")
            return
        img_data, crop_msg = await asyncio.to_thread(self._crop_image_data, img_data, margins)
        func = self.process_mode_1 if algorithm_mode == 1 else self.process_mode_2
        res_msg, gif_bytes = await asyncio.to_thread(func, img_data, rows, cols, duration)
        if gif_bytes:
            yield event.chain_result([Comp.Plain(res_msg + crop_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
        else:
            yield event.plain_result(f"❌ 失败：\n{res_msg}")

    @filter.command("合成1gif")
    async def make_gif_v1(self, event: AstrMessageEvent):
        async for res in self._handle_gif_task(event, 1): yield res

    @filter.command("合成2gif")
    async def make_gif_v2(self, event: AstrMessageEvent):
        async for res in self._handle_gif_task(event, 2): yield res

    def process_mode_1(self, img_data: bytes, rows: int, cols: int, duration_sec: float):
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if getattr(img, "is_animated", False): img.seek(0)
            img = img.convert("RGBA")
            w, h = img.size
            cw, ch = w // cols, h // rows
            if cw < 2 or ch < 2: return f"⚠️ 单格太小 ({cw}x{ch})", None
            frames = []
            for r in range(rows):
                for c in range(cols):
                    frames.append(img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)))
            output = io.BytesIO()
            self._save_animation(output, frames, int(duration_sec * 1000), loop=0)
            output.seek(0)
            return f"✅ 合成成功\n算法1 | {w}x{h} | {rows}行{cols}列", output
        except Exception as e:
            return f"逻辑异常: {e}", None

    def process_mode_2(self, img_data: bytes, rows: int, cols: int, duration_sec: float):
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if getattr(img, "is_animated", False): img.seek(0)
            img = img.convert("RGBA")
            datas = img.getdata()
            new_data = [(0, 0, 0, 0) if item[3] < 128 else (item[0], item[1], item[2], 255) for item in datas]
            img.putdata(new_data)
            has_trans = any(d[3] == 0 for d in new_data)
            master_pal = img.convert("RGB").quantize(colors=255 if has_trans else 256, method=1)
            w, h = img.size
            cw, ch = w // cols, h // rows
            if cw < 2 or ch < 2: return f"⚠️ 单格太小 ({cw}x{ch})", None
            frames = []
            for r in range(rows):
                for c in range(cols):
                    crop = img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch))
                    frame = crop.convert("RGB").quantize(palette=master_pal)
                    if has_trans:
                        mask = crop.split()[3].point(lambda a: 255 if a < 128 else 0)
                        frame.paste(255, mask=mask)
                    frames.append(frame)
            output = io.BytesIO()
            fmt = self.cfg.get('output_format', 'GIF').upper()
            if fmt == 'GIF':
                frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:],
                               duration=int(duration_sec * 1000), loop=0, disposal=2,
                               transparency=255 if has_trans else None, optimize=True)
            else:
                self._save_animation(output, frames, int(duration_sec * 1000), loop=0)
            output.seek(0)
            return f"✅ 合成成功\n算法2 | {w}x{h} | {rows}行{cols}列", output
        except Exception as e:
            return f"逻辑异常: {e}", None

    # --- 统一变速命令：/gif变速 ---
    @filter.command("gif变速")
    async def gif_speed_change(self, event: AstrMessageEvent):
        msg = event.message_str
        is_fps_mode = False
        speed_factor = 2.0
        mode_desc = ""

        # 1. 解析 fps 模式：/gif变速 60fps 或 /gif变速 60帧
        fps_match = re.search(r'(\d+\.?\d*)\s*(?:fps|帧)', msg, re.I)
        if fps_match:
            speed_factor = float(fps_match.group(1))
            speed_factor = max(1.0, min(speed_factor, 120.0))
            is_fps_mode = True
            mode_desc = f"{speed_factor:.0f}fps"
        else:
            # 2. 解析倍数模式：/gif变速 2x 或 /gif变速 0.5倍 或纯数字
            mult_match = re.search(r'(\d+\.?\d*)\s*[xX×倍]', msg)
            if mult_match:
                speed_factor = float(mult_match.group(1))
            else:
                num_match = re.search(r'(\d+\.?\d*)', msg)
                if num_match:
                    speed_factor = float(num_match.group(1))
            speed_factor = max(0.1, min(speed_factor, 20.0))
            mode_desc = f"{speed_factor}x"

        img_url = self._get_image_url(event)
        if not img_url:
            yield event.plain_result("❌ 请发送GIF或回复GIF\n用法: /gif变速 2x (倍数)  或  /gif变速 30fps (目标帧率)")
            return

        yield event.plain_result(f"⏳ 正在变速 {mode_desc}...")
        img_data = await self._download_image(img_url)
        if not img_data:
            yield event.plain_result("❌ 下载失败")
            return

        res_msg, gif_bytes = await asyncio.to_thread(self.process_speed, img_data, speed_factor, is_fps_mode)
        if gif_bytes:
            yield event.chain_result([Comp.Plain(res_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
        else:
            yield event.plain_result(f"❌ 失败：{res_msg}")

    # --- 兼容旧命令：加速 / 减速（转发到变速逻辑） ---
    async def _legacy_speed_impl(self, event: AstrMessageEvent, is_accelerate: bool):
        msg = event.message_str
        factor = 2.0
        num_match = re.search(r"(\d+\.?\d*)", msg)
        if num_match:
            factor = float(num_match.group(1))
        factor = max(0.1, min(factor, 20.0))
        speed_factor = factor if is_accelerate else (1.0 / factor)
        action_name = "加速" if is_accelerate else "减速"

        img_url = self._get_image_url(event)
        if not img_url:
            return
        yield event.plain_result(f"⏳ 正在处理 {action_name} {factor}倍...")
        img_data = await self._download_image(img_url)
        if not img_data:
            yield event.plain_result("❌ 下载失败")
            return
        res_msg, gif_bytes = await asyncio.to_thread(self.process_speed, img_data, speed_factor, is_fps_mode=False)
        if gif_bytes:
            yield event.chain_result([Comp.Plain(res_msg), Comp.Image.fromBytes(gif_bytes.getvalue())])
        else:
            yield event.plain_result(f"❌ 失败：{res_msg}")

    @filter.command("加速")
    async def accelerate_gif(self, event: AstrMessageEvent):
        async for res in self._legacy_speed_impl(event, True): yield res

    @filter.command("减速")
    async def decelerate_gif(self, event: AstrMessageEvent):
        async for res in self._legacy_speed_impl(event, False): yield res

    # --- 变速核心算法（支持倍数/目标fps两种模式，超过50fps自动抽帧） ---
    MAX_FPS = 50

    def process_speed(self, img_data: bytes, speed_factor: float, is_fps_mode: bool = False):
        """
        speed_factor:
          - is_fps_mode=False: 倍数模式，1.0=原速, 2.0=2倍速, 0.5=半速
          - is_fps_mode=True:  目标fps模式，如 30fps, 60fps
        当变速后帧率超过50fps时自动抽帧降帧率。
        """
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if not getattr(img, "is_animated", False):
                return "这不是GIF/动图", None

            # 收集所有帧及其原始时长
            frames, orig_durs = [], []
            for frame in ImageSequence.Iterator(img):
                orig_durs.append(frame.info.get('duration', 100))
                frames.append(frame.copy())

            if not frames:
                return "无有效帧", None

            # 计算原始平均帧率
            avg_orig_dur = sum(orig_durs) / len(orig_durs)
            orig_fps = 1000.0 / avg_orig_dur if avg_orig_dur > 0 else 10.0

            # 根据模式计算 duration 缩放比
            if is_fps_mode:
                target_fps = speed_factor
                ratio = orig_fps / target_fps if target_fps > 0 else 1.0
            else:
                ratio = 1.0 / speed_factor  # 2x → ratio=0.5（时长减半）

            # 应用变速，每帧最短20ms
            new_durs = [max(20, int(d * ratio)) for d in orig_durs]

            # 判断是否需要抽帧：变速后平均帧率 > MAX_FPS
            avg_new_dur = sum(new_durs) / len(new_durs)
            avg_new_fps = 1000.0 / avg_new_dur if avg_new_dur > 0 else self.MAX_FPS

            dropped = False
            original_count = len(frames)
            if avg_new_fps > self.MAX_FPS:
                step = max(2, round(avg_new_fps / self.MAX_FPS))
                keep_frames, keep_durs = [], []
                for i in range(0, len(frames), step):
                    keep_frames.append(frames[i])
                    keep_durs.append(new_durs[i])
                frames, new_durs = keep_frames, keep_durs
                dropped = True

            output = io.BytesIO()
            frames[0].save(output, format='GIF', save_all=True, append_images=frames[1:],
                           duration=new_durs, loop=0, disposal=2, optimize=True)
            output.seek(0)

            # 构建结果消息
            mode_str = f"{speed_factor:.0f}fps" if is_fps_mode else f"{speed_factor}x"
            msg = f"✅ 变速完成 ({mode_str})"
            if dropped:
                msg += f"\n💡 帧率超{self.MAX_FPS}fps，已自动抽帧({original_count}→{len(frames)}帧)"
            return msg, output
        except Exception as e:
            return f"异常: {e}", None

    def _worker_crop_grid(self, img_data: bytes, margins: dict, rows: int, cols: int):
        img_data, crop_msg = self._crop_image_data(img_data, margins)
        try:
            img = PILImage.open(io.BytesIO(img_data)).convert("RGBA")
            w, h = img.size
            cw, ch = w // cols, h // rows
            if cw < 1 or ch < 1: return f"❌ 图片太小 {crop_msg}", None
            res_list = []
            for r in range(rows):
                for c in range(cols):
                    out = io.BytesIO()
                    img.crop((c * cw, r * ch, (c + 1) * cw, (r + 1) * ch)).save(out, format='PNG')
                    res_list.append(out.getvalue())
            return crop_msg, res_list
        except Exception as e:
            return f"❌ 出错: {e}", None

    @filter.command("裁剪")
    async def crop_and_forward(self, event: AstrMessageEvent):
        clean, margins = self._parse_margins(event.message_str)
        match = re.search(r'(\d+)\s*[*x×]\s*(\d+)', clean)
        rows, cols = (int(match.group(1)), int(match.group(2))) if match else (1, 1)
        if rows > 20 or cols > 20:
            yield event.plain_result("⚠️ 行列数过大")
            return
        img_url = self._get_image_url(event)
        if not img_url:
            yield event.plain_result("❌ 请发送图片")
            return
        yield event.plain_result("⏳ 处理中...")
        img_data = await self._download_image(img_url)
        if not img_data:
            yield event.plain_result("❌ 下载失败")
            return
        msg, bytes_list = await asyncio.to_thread(self._worker_crop_grid, img_data, margins, rows, cols)
        if not bytes_list:
            yield event.plain_result(msg)
            return
        nodes = [Comp.Node(name="裁剪", content=[Comp.Plain(f"结果 {rows}x{cols}{msg}")])]
        for b in bytes_list:
            nodes.append(Comp.Node(name="裁剪", content=[Comp.Image.fromBytes(b)]))
        yield event.chain_result([Comp.Nodes(nodes=nodes)])

    @filter.command("gif分解")
    async def decompose_gif(self, event: AstrMessageEvent):
        img_url = self._get_image_url(event)
        if not img_url:
            yield event.plain_result("❌ 请发送GIF")
            return
        yield event.plain_result("⏳ 分解中...")
        img_data = await self._download_image(img_url)
        frames = await asyncio.to_thread(self._worker_decompose, img_data)
        if isinstance(frames, str):
            yield event.plain_result(frames)
            return
        nodes = [Comp.Node(name="GIF助手", content=[Comp.Plain(f"第{i + 1}帧"), Comp.Image.fromBytes(b)]) for i, b in
                 enumerate(frames)]
        yield event.chain_result([Comp.Nodes(nodes=nodes)])

    def _worker_decompose(self, img_data: bytes):
        try:
            img = PILImage.open(io.BytesIO(img_data))
            if not getattr(img, "is_animated", False): return "⚠️ 不是GIF动画"
            frames = []
            for i, frame in enumerate(ImageSequence.Iterator(img)):
                if i >= 100: break
                out = io.BytesIO()
                frame.copy().convert("RGBA").save(out, format='PNG')
                frames.append(out.getvalue())
            return frames
        except Exception as e:
            return f"❌ 出错: {e}"

    # --- 新增: 多图合成 GIF 核心处理逻辑 ---
    def _worker_multi_image_gif(self, images_bytes: list[bytes], duration_sec: float):
        try:
            pil_images = []
            max_w, max_h = 0, 0

            # 1. 加载所有图片并计算最大尺寸
            for b in images_bytes:
                try:
                    img = PILImage.open(io.BytesIO(b)).convert("RGBA")
                    # 如果是动态图，取第一帧
                    if getattr(img, "is_animated", False):
                        img.seek(0)
                        img = img.copy()
                    pil_images.append(img)
                    max_w = max(max_w, img.width)
                    max_h = max(max_h, img.height)
                except Exception as e:
                    logger.warning(f"加载图片失败: {e}")

            if not pil_images:
                return "❌ 没有有效的图片", None

            frames = []
            # 2. 统一尺寸：保持比例缩放，居中填充
            for img in pil_images:
                # 创建透明背景（如果合成JPG可以改为白色背景）
                bg = PILImage.new("RGBA", (max_w, max_h), (255, 255, 255, 0))

                # 计算缩放比例
                src_ratio = img.width / img.height
                tgt_ratio = max_w / max_h

                if src_ratio > tgt_ratio:
                    # 按照宽度缩放
                    new_w = max_w
                    new_h = int(max_w / src_ratio)
                else:
                    # 按照高度缩放
                    new_h = max_h
                    new_w = int(max_h * src_ratio)

                # 缩放图片
                img_resized = img.resize((new_w, new_h), PILImage.Resampling.BILINEAR)

                # 居中粘贴
                paste_x = (max_w - new_w) // 2
                paste_y = (max_h - new_h) // 2
                bg.paste(img_resized, (paste_x, paste_y), mask=img_resized if 'A' in img_resized.getbands() else None)

                # 将透明部分处理为白色（对于GIF显示效果更好，或者保留透明）
                # 这里为了通用性，如果输出GIF，Pillow会自动处理透明度。
                # 如果希望背景是白色：
                # final_frame = PILImage.new("RGB", (max_w, max_h), (255, 255, 255))
                # final_frame.paste(bg, mask=bg.split()[3])
                frames.append(bg)

            # 3. 保存动画
            output = io.BytesIO()
            duration_ms = int(duration_sec * 1000)
            self._save_animation(output, frames, duration_ms, loop=0)
            output.seek(0)

            return f"✅ 合成成功 ({len(frames)}张)", output

        except Exception as e:
            return f"合成出错: {repr(e)}", None

    # --- 新增: 表情包做旧功能 (模拟早期互联网传播效果) ---
    def _worker_age_meme(self, img_data: bytes, times: int) -> tuple[str, bytes]:
        """
        模拟早期互联网图片传播的做旧效果:
        1. 绿色通道增强 (变绿)
        2. 低质量JPEG反复压缩 (马赛克失真)
        3. 模糊处理 (变糊)
        4. 饱和度/对比度调整 (颜色脏化)
        自动检测GIF并逐帧处理后重新合成
        """
        try:
            img = PILImage.open(io.BytesIO(img_data))
            
            # 自动检测是否是动图 (GIF/APNG/WebP动图)
            is_animated = getattr(img, "is_animated", False)
            
            if is_animated:
                # === 处理动图: 分解 -> 逐帧做旧 -> 重新合成 ===
                frames = []
                durations = []
                
                # 获取所有帧
                for frame in ImageSequence.Iterator(img):
                    dur = frame.info.get('duration', 100)
                    if dur <= 0:
                        dur = 100
                    durations.append(dur)
                    # 复制帧并转换为RGB进行做旧处理
                    frame_copy = frame.copy().convert("RGB")
                    aged_frame = self._age_single_frame(frame_copy, times)
                    # 转换回P模式以便GIF保存 (带调色板)
                    frames.append(aged_frame)
                
                if not frames:
                    return "❌ 无法读取动图帧", None
                
                # 将RGB帧转换为调色板模式以生成GIF
                gif_frames = []
                for f in frames:
                    # 量化为256色
                    p_frame = f.convert("P", palette=PILImage.Palette.ADAPTIVE, colors=256)
                    gif_frames.append(p_frame)
                
                output = io.BytesIO()
                gif_frames[0].save(
                    output, 
                    format='GIF', 
                    save_all=True, 
                    append_images=gif_frames[1:],
                    duration=durations, 
                    loop=0, 
                    disposal=2, 
                    optimize=False
                )
                output.seek(0)
                return f"✅ 做旧成功 (动图 {len(frames)}帧, {times}次传播)", output.getvalue()
            else:
                # === 静态图处理 ===
                img = img.convert("RGB")
                aged_img = self._age_single_frame(img, times)
                
                output = io.BytesIO()
                # 最终以中低质量JPEG保存，增加"古早"感
                final_quality = max(30, 70 - times * 3)
                aged_img.save(output, format='JPEG', quality=final_quality)
                return f"✅ 做旧成功 ({times}次传播, 质量{final_quality}%)", output.getvalue()
                
        except Exception as e:
            import traceback
            return f"❌ 处理失败: {repr(e)}\n{traceback.format_exc()}", None

    def _age_single_frame(self, img: PILImage.Image, times: int) -> PILImage.Image:
        """对单帧图片进行做旧处理 - 渐进式做旧"""
        import random
        
        # 确保是RGB模式
        if img.mode != "RGB":
            img = img.convert("RGB")
        
        for i in range(times):
            # === 1. 绿色通道偏移 (变绿) - 渐进式，不是每次都加 ===
            # 只在特定轮次进行色彩偏移，让变化更加渐进
            if i % 3 == 0:  # 每3次做一次色彩偏移
                r, g, b = img.split()
                
                # 非常轻微的绿色增强 (每次只加1-2)
                green_boost = random.randint(1, 2)
                red_reduce = random.randint(0, 1)
                blue_reduce = random.randint(0, 1)
                
                # 使用函数工厂避免闭包问题
                def make_add_func(val):
                    return lambda x: min(255, x + val)
                def make_sub_func(val):
                    return lambda x: max(0, x - val)
                
                g = g.point(make_add_func(green_boost))
                if red_reduce > 0:
                    r = r.point(make_sub_func(red_reduce))
                if blue_reduce > 0:
                    b = b.point(make_sub_func(blue_reduce))
                
                img = PILImage.merge("RGB", (r, g, b))
            
            # === 2. JPEG压缩失真 (核心做旧效果) ===
            # 模拟多次保存/转发的压缩损失
            # 质量从70逐渐降到25，变化更平缓
            quality = max(25, 70 - i * 3)
            temp_io = io.BytesIO()
            img.save(temp_io, format='JPEG', quality=quality)
            temp_io.seek(0)
            img = PILImage.open(temp_io).convert("RGB")
            
            # === 3. 轻微模糊 (变糊) - 每3次做一次 ===
            if i % 3 == 0:
                blur_radius = 0.2 + (i // 3) * 0.1  # 非常轻微的模糊
                img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
            
            # === 4. 轻微锐化 (模拟过度锐化的"塑料感") - 偶尔做 ===
            if i % 5 == 2:
                img = img.filter(ImageFilter.SHARPEN)
            
            # === 5. 轻微降低饱和度 (颜色变脏) ===
            # 变化更加平缓
            if i % 2 == 0:
                enhancer = ImageEnhance.Color(img)
                saturation = max(0.85, 1.0 - 0.015)  # 每次只降1.5%
                img = enhancer.enhance(saturation)
            
            # === 6. 轻微降低对比度 (变灰暗) ===
            if i % 2 == 1:
                enhancer = ImageEnhance.Contrast(img)
                contrast = max(0.85, 1.0 - 0.01)  # 每次只降1%
                img = enhancer.enhance(contrast)
            
            # === 7. 缩放再放大 (像素化) - 仅在高次数时 ===
            if times >= 15 and i == times // 2:
                w, h = img.size
                if w > 50 and h > 50:
                    small = img.resize((int(w * 0.8), int(h * 0.8)), PILImage.Resampling.BILINEAR)
                    img = small.resize((w, h), PILImage.Resampling.BILINEAR)
        
        return img

    @filter.command("表情包做旧")
    async def age_meme(self, event: AstrMessageEvent):
        """
        表情包做旧功能，模拟早期互联网图片传播效果
        用法：表情包做旧 [次数]
        示例：表情包做旧 10 (做旧10次，数字越大越绿越糊)
        建议：1-5次轻度做旧，5-10次中度做旧，10-20次重度做旧
        """
        msg_text = event.message_str
        
        # 解析做旧次数
        times = 5  # 默认5次
        num_match = re.search(r'做旧\s*(\d+)', msg_text)
        if num_match:
            times = int(num_match.group(1))
        else:
            # 尝试匹配其他数字
            num_match = re.search(r'(\d+)', msg_text)
            if num_match:
                times = int(num_match.group(1))
        
        # 限制范围
        times = max(1, min(times, 50))  # 1-50次
        
        img_url = self._get_image_url(event)
        if not img_url:
            yield event.plain_result("❌ 请发送图片或回复图片\n用法: 表情包做旧 [次数]\n次数越大越绿越糊 (建议1-20)")
            return
        
        # 根据次数给出提示
        if times <= 5:
            level = "轻度做旧 (微微泛绿)"
        elif times <= 10:
            level = "中度做旧 (明显发绿变糊)"
        elif times <= 20:
            level = "重度做旧 (经典老图风格)"
        else:
            level = "极限做旧 (赛博遗产级别)"
        
        yield event.plain_result(f"⏳ 正在做旧... ({times}次传播, {level})")
        
        img_data = await self._download_image(img_url)
        if not img_data:
            yield event.plain_result("❌ 图片下载失败")
            return
        
        # 自动检测动图类型并处理
        res_msg, result_bytes = await asyncio.to_thread(
            self._worker_age_meme, img_data, times
        )
        
        if result_bytes:
            yield event.chain_result([
                Comp.Plain(f"{res_msg}\n💡 {level}"),
                Comp.Image.fromBytes(result_bytes)
            ])
        else:
            yield event.plain_result(res_msg)

    @filter.command("多图合成gif")
    async def multi_img_gif(self, event: AstrMessageEvent):
        """
        多图合成GIF，支持直接发送图片、回复含图消息、转发消息。
        用法：多图合成gif [速度/时长]
        示例：多图合成gif 0.5 (每帧0.5秒)
        """
        # 1. 解析参数 (每帧时长)
        msg_text = event.message_str.replace("多图合成gif", "")
        duration = 0.5  # 默认0.5秒

        # 尝试匹配 fps (例如 10fps) -> 转为 duration
        fps_match = re.search(r'(\d+)\s*(?:fps|帧)', msg_text, re.I)
        if fps_match:
            try:
                fps = float(fps_match.group(1))
                if fps > 0: duration = 1.0 / fps
            except:
                pass
        else:
            # 尝试匹配秒数 (例如 0.2)
            sec_match = re.search(r'(\d+(?:\.\d+)?)', msg_text)
            if sec_match:
                try:
                    val = float(sec_match.group(1))
                    if 0.01 <= val <= 60: duration = val
                except:
                    pass

        yield event.plain_result("⏳ 正在搜集图片资源...")

        # 2. 获取所有图片链接
        img_urls = await self._get_all_image_urls(event)

        if not img_urls or len(img_urls) < 1:
            yield event.plain_result("❌ 未检测到足够的图片资源 (请回复图片消息，或发送包含图片的合并转发)")
            return

        yield event.plain_result(f"⏳ 正在下载 {len(img_urls)} 张图片并合成 (每帧{duration:.2f}s)...")

        # 3. 并发下载图片
        tasks = [self._download_content(url) for url in img_urls]
        results = await asyncio.gather(*tasks)
        valid_bytes = [b for b in results if b is not None]

        if len(valid_bytes) < 1:  # 允许单张图变成GIF (静止或只有一帧)
            yield event.plain_result("❌ 图片下载失败")
            return

        # 4. 执行合成
        res_msg, gif_io = await asyncio.to_thread(self._worker_multi_image_gif, valid_bytes, duration)

        if gif_io:
            yield event.chain_result([
                Comp.Plain(f"{res_msg}\n画布适应最大尺寸，自动居中填充"),
                Comp.Image.fromBytes(gif_io.getvalue())
            ])
        else:
            yield event.plain_result(res_msg)