"""
网络请求优化工具类
用于优化字体下载和其他网络操作，防止阻塞和超时
"""
import asyncio
import aiohttp
import os
import time
from typing import Optional, Dict, Any, Tuple
from pathlib import Path
import hashlib
import subprocess
from app.core.config import settings
from app.log import logger
from app.utils.http import RequestUtils
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class NetworkHelper:
    """网络请求助手类，提供超时控制和重试机制"""

    def __init__(self, timeout: int = 30, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = None

    @staticmethod
    def _proxy_env() -> dict:
        """
        将 MoviePilot 的代理配置同步给 wget 兜底下载。
        RequestUtils 会直接使用 settings.PROXY，这里只处理子进程环境变量。
        """
        env = os.environ.copy()
        proxies = settings.PROXY or {}
        if isinstance(proxies, dict):
            http_proxy = proxies.get("http")
            https_proxy = proxies.get("https") or http_proxy
            if http_proxy:
                env["http_proxy"] = http_proxy
                env["HTTP_PROXY"] = http_proxy
            if https_proxy:
                env["https_proxy"] = https_proxy
                env["HTTPS_PROXY"] = https_proxy
        return env

    async def __aenter__(self):
        """异步上下文管理器入口"""
        connector = aiohttp.TCPConnector(
            limit=10,
            limit_per_host=5,
            ttl_dns_cache=300,
            use_dns_cache=True,
        )
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={'User-Agent': 'MoviePilot-MediaCoverGenerator/1.0'},
            trust_env=True
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        if self.session:
            await self.session.close()

    async def download_file_async(self, url: str, save_path: Path,
                                 expected_size: Optional[int] = None) -> bool:
        """
        异步下载文件

        Args:
            url: 下载链接
            save_path: 保存路径
            expected_size: 期望的文件大小（字节）

        Returns:
            bool: 下载是否成功
        """
        if not self.session:
            raise RuntimeError("NetworkHelper must be used as async context manager")

        for attempt in range(self.max_retries):
            try:
                logger.info(f"开始下载文件 (尝试 {attempt + 1}/{self.max_retries}): {url}")

                async with self.session.get(url) as response:
                    if response.status == 200:
                        content = await response.read()

                        # 验证文件大小
                        if expected_size and len(content) != expected_size:
                            logger.warning(f"文件大小不匹配: 期望 {expected_size}, 实际 {len(content)}")
                            if attempt < self.max_retries - 1:
                                continue

                        # 确保目录存在
                        save_path.parent.mkdir(parents=True, exist_ok=True)

                        # 写入文件
                        with open(save_path, 'wb') as f:
                            f.write(content)

                        logger.info(f"文件下载成功: {save_path}")
                        return True
                    else:
                        logger.warning(f"下载失败，HTTP状态码: {response.status}")

            except asyncio.TimeoutError:
                logger.warning(f"下载超时 (尝试 {attempt + 1}/{self.max_retries}): {url}")
            except Exception as e:
                logger.warning(f"下载出错 (尝试 {attempt + 1}/{self.max_retries}): {e}")

            if attempt < self.max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # 指数退避

        logger.error(f"文件下载失败，已重试 {self.max_retries} 次: {url}")
        return False

    def download_file_sync(self, url: str, save_path: Path,
                          expected_size: Optional[int] = None) -> bool:
        """
        同步下载文件（带超时控制）

        Args:
            url: 下载链接
            save_path: 保存路径
            expected_size: 期望的文件大小（字节）

        Returns:
            bool: 下载是否成功
        """
        for attempt in range(self.max_retries):
            try:
                logger.info(f"开始下载文件 (尝试 {attempt + 1}/{self.max_retries}): {url}")

                req = RequestUtils(
                    headers={'User-Agent': 'MoviePilot-MediaCoverGenerator/1.0'},
                    proxies=settings.PROXY,
                    timeout=self.timeout,
                )
                with req.get_stream(url) as response:
                    if response is None:
                        logger.warning("下载失败，未获取到响应")
                        continue

                    if response.status_code != 200:
                        logger.warning(f"下载失败，HTTP状态码: {response.status_code}")
                        continue

                    content = response.content

                    # 验证文件大小
                    if expected_size and len(content) != expected_size:
                        logger.warning(f"文件大小不匹配: 期望 {expected_size}, 实际 {len(content)}")
                        if attempt < self.max_retries - 1:
                            continue

                    # 确保目录存在
                    save_path.parent.mkdir(parents=True, exist_ok=True)

                    # 写入文件
                    with open(save_path, 'wb') as f:
                        f.write(content)

                    logger.info(f"文件下载成功: {save_path}")
                    return True
            except Exception as e:
                logger.warning(f"下载出错 (尝试 {attempt + 1}/{self.max_retries}): {e}")

            if attempt < self.max_retries - 1:
                time.sleep(2 ** attempt)  # 指数退避

        # Python下载失败，尝试使用wget
        try:
            logger.info(f"Python下载失败，尝试使用系统 wget 命令: {url}")
            save_path.parent.mkdir(parents=True, exist_ok=True)
            
            # wget 命令 -q 安静模式 -O 输出文件
            cmd = ["wget", "-O", str(save_path), url]
            if "github.com" in url or "raw.githubusercontent.com" in url:
                # GitHub 下载可能需要关闭证书验证
                cmd.append("--no-check-certificate")
                
            subprocess.run(
                cmd,
                check=True,
                timeout=self.timeout * 2,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._proxy_env(),
            )
            
            if save_path.exists() and save_path.stat().st_size > 0:
                if expected_size and save_path.stat().st_size != expected_size:
                    logger.warning(f"wget下载文件大小不匹配")
                    return False
                logger.info(f"wget 下载成功: {save_path}")
                return True
        except Exception as e:
            logger.error(f"wget 下载也失败: {e}")

        logger.error(f"文件下载失败，已重试 {self.max_retries} 次 + wget: {url}")
        return False


def validate_font_file(font_path: Path, sample_text: Optional[str] = None, strict_render: bool = False) -> bool:
    """
    验证字体文件是否有效

    Args:
        font_path: 字体文件路径
        sample_text: 可选的示例文字；传入后会额外诊断字体是否能实际渲染出可见像素
        strict_render: 是否将示例文字不可见视为字体无效

    Returns:
        bool: 字体文件是否有效
    """
    try:
        if not font_path.exists() or font_path.stat().st_size == 0:
            return False

        # 尝试加载字体文件
        from PIL import Image, ImageDraw, ImageFont
        font = ImageFont.truetype(str(font_path), 24)
        if sample_text:
            bbox = font.getbbox(sample_text)
            text_w = max(1, bbox[2] - bbox[0])
            text_h = max(1, bbox[3] - bbox[1])
            test_img = Image.new("RGBA", (text_w + 32, text_h + 32), (0, 0, 0, 0))
            draw = ImageDraw.Draw(test_img)
            draw.text((16 - bbox[0], 16 - bbox[1]), sample_text, font=font, fill=(255, 255, 255, 255))
            if not test_img.getchannel("A").getbbox():
                message = f"字体文件示例文字渲染不可见: {font_path}, sample={sample_text}"
                if strict_render:
                    logger.warning(message)
                    return False
                logger.debug(f"{message}，已降级为仅校验字体可加载")
        return True
    except Exception as e:
        logger.warning(f"字体文件验证失败: {font_path}, 错误: {e}")
        return False


def get_file_hash(file_path: Path) -> Optional[str]:
    """
    计算文件的MD5哈希值

    Args:
        file_path: 文件路径

    Returns:
        str: 文件的MD5哈希值，如果文件不存在则返回None
    """
    try:
        if not file_path.exists():
            return None

        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception as e:
        logger.warning(f"计算文件哈希失败: {file_path}, 错误: {e}")
        return None
