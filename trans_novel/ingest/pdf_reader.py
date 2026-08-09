"""PDF 读取器：PDF → MinerU API → HTML → read_html → Document。

流程：
1. 将 PDF 转换为 HTML（调用 MinerU Precision API），中间产物保存在运行状态目录
2. 若中间 HTML 已存在则跳过转换（便于人工检查/修改后重跑）
3. 用 html_reader 将 HTML 解析为 Document，再覆盖 fmt="pdf" 与原始路径

依赖：
  转换 PDF 需要 httpx / pypdf；缺失时给出安装提示。
  若已有中间 HTML 则不需要这些依赖。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from pathlib import Path

from .errors import MinerUError
from .html_reader import read_html
from .models import Document


def _source_sha256(path: str) -> str:
    """流式计算 PDF 内容哈希，供直接调用读取器时绑定缓存。"""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_cache_html_path(cache_dir: str, source_hash: str) -> str:
    """返回由源文件哈希隔离的 MinerU HTML 缓存路径。"""
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("源文件 SHA-256 格式无效")
    return os.path.join(cache_dir, source_hash, "converted.html")


def _check_deps() -> None:
    """检查 PDF 转换所需的可选依赖，缺失时给出安装提示。"""
    missing = []
    for mod, pkg in [("httpx", "httpx"), ("pypdf", "pypdf")]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if missing:
        raise ImportError(
            f"PDF 转换需要额外依赖，请运行：\n"
            f"  uv pip install {' '.join(missing)}\n"
            f"也可先安装依赖完成一次转换，再检查状态目录 source/<SHA-256>/"
            f"converted.html。"
        )


def read_pdf(
    path: str,
    source_lang: str,
    target_lang: str,
    *,
    cache_dir: str,
    source_hash: str | None = None,
    api_token: str | None = None,
) -> Document:
    """将 PDF 转换为 HTML 后解析为 Document。

    中间 HTML 产物保存在本书运行状态目录的
    ``source/<source_sha256>/converted.html``，
    便于人工检查 MinerU 解析质量。若已存在则直接复用，不重复调用 API。

    Parameters
    ----------
    path : str
        PDF 文件路径。
    source_lang : str
        源语言代码。
    target_lang : str
        目标语言代码。
    cache_dir : str
        本书运行状态下的输入预处理缓存目录。
    source_hash : str | None
        调用方已计算的源文件 SHA-256；省略时由读取器计算。
    api_token : str | None
        MinerU API token，默认读环境变量 ``MINERU_API_KEY``。

    Returns
    -------
    Document
        fmt="pdf"，source_path 指向原始 PDF。
    """
    digest = source_hash or _source_sha256(path)
    html_path = pdf_cache_html_path(cache_dir, digest)
    os.makedirs(os.path.dirname(html_path), exist_ok=True)

    converted = False
    # 若中间 HTML 不存在，调用 MinerU 转换
    if not os.path.isfile(html_path):
        _check_deps()
        from .pdf_to_html import convert_pdf_to_html

        temporary_html_path = f"{html_path}.tmp"
        try:
            os.remove(temporary_html_path)
        except FileNotFoundError:
            pass
        try:
            convert_pdf_to_html(path, temporary_html_path, api_token=api_token)
            os.replace(temporary_html_path, html_path)
            converted = True
        except MinerUError:
            shutil.rmtree(os.path.dirname(html_path), ignore_errors=True)
            raise
        except Exception as error:
            shutil.rmtree(os.path.dirname(html_path), ignore_errors=True)
            # HTTP、PDF 解析、ZIP 解包和写盘失败统一为输入层异常；
            # 原异常作为 cause 保留，便于调试时追踪。
            raise MinerUError(f"PDF 转换失败：{error}") from error

    # 用 html_reader 解析中间 HTML
    doc = read_html(html_path, source_lang, target_lang)
    if _source_sha256(path) != digest:
        if converted:
            shutil.rmtree(os.path.dirname(html_path), ignore_errors=True)
        raise ValueError("PDF 在转换或解析期间发生变化；已放弃本次缓存，请重试。")

    # 覆盖为 PDF 原始信息
    doc.title = Path(path).stem
    doc.fmt = "pdf"
    doc.source_path = os.path.abspath(path)

    return doc
