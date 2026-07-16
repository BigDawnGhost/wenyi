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

import os
from pathlib import Path

from .errors import MinerUError
from .html_reader import read_html
from .models import Document


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
            f"或先手动将 PDF 转为 HTML，保存到本书状态目录的 "
            f"source/converted.html，再重跑。"
        )


def read_pdf(
    path: str,
    source_lang: str,
    target_lang: str,
    *,
    cache_dir: str,
    api_token: str | None = None,
) -> Document:
    """将 PDF 转换为 HTML 后解析为 Document。

    中间 HTML 产物保存在本书运行状态目录的
    ``source/converted.html``，
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
    api_token : str | None
        MinerU API token，默认读环境变量 ``MINERU_API_KEY``。

    Returns
    -------
    Document
        fmt="pdf"，source_path 指向原始 PDF。
    """
    os.makedirs(cache_dir, exist_ok=True)
    html_path = os.path.join(cache_dir, "converted.html")

    # 若中间 HTML 不存在，调用 MinerU 转换
    if not os.path.isfile(html_path):
        _check_deps()
        from .pdf_to_html import convert_pdf_to_html  # noqa: E402

        try:
            convert_pdf_to_html(path, html_path, api_token=api_token)
        except MinerUError:
            raise
        except Exception as error:
            # HTTP、PDF 解析、ZIP 解包和写盘失败统一为输入层异常；
            # 原异常作为 cause 保留，便于调试时追踪。
            raise MinerUError(f"PDF 转换失败：{error}") from error

    # 用 html_reader 解析中间 HTML
    doc = read_html(html_path, source_lang, target_lang)

    # 覆盖为 PDF 原始信息
    doc.title = Path(path).stem
    doc.fmt = "pdf"
    doc.source_path = os.path.abspath(path)
    doc.meta["pdf_path"] = doc.source_path
    doc.meta["converted_html_path"] = os.path.abspath(html_path)

    return doc
