"""配置加载。读取 config.yaml，提供带默认值的类型化访问（pydantic v2）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


_DEFAULT_CONFIG_YAML = """\
# trans-novel 配置（多语言小说 → 中文）
# 修改后无需改代码；模型提供商、流水线和输出开关都在这里。

language:
  source: auto # auto 由模型识别来源语言；也可写死 ja / en / ko / ru / de 等语言代码
  target: zh # 译文语言

# ── LLM ──────────────────────────────────────────────────────────────────
llm:
  # deepseek | openai | openrouter | openai-compatible | ollama | vllm | fake
  provider: deepseek
  base_url: https://api.deepseek.com
  api_key_env: DEEPSEEK_API_KEY
  timeout: 600
  max_retries: 4
  tiers:
    strong:
      model: deepseek-v4-pro
      options:
        thinking: true
        reasoning_effort: high
    cheap:
      model: deepseek-v4-flash
      options:
        thinking: true
        reasoning_effort: high
    fast:
      model: deepseek-v4-flash
      options:
        thinking: false

# ── 切分 ─────────────────────────────────────────────────────────────────
segment:
  # 一个翻译批次（句群）的目标大小，按字符粗略估算 token。
  max_chars_per_batch: 1800
  # 单个段落超过该长度时按句末标点再切成多段（续段回填时并回同段），避免超长段。
  max_chars_per_segment: 1200

# ── 流水线开关（质量/成本平衡）───────────────────────────────────────────
pipeline:
  review: true # 章末整章审校（廉价档）：漏译/误译/术语/人称检查
  autofix_severe: false # 审校后自动重译严重项（漏译/误译）；关掉仅上报，不改正文
  align_retry_limit: 2
  polish: true # 润色（强档）：等于用 pro 把全书再翻一遍，最烧钱；默认开
  backtranslate_sample: 0 # 回译抽检比例（0 关闭）
  consistency_qa: false # 全书跨章一致性收尾扫描
  rolling_context_segments: 6 # 注入的前文译文尾段数
  book_understanding: true # 翻译前预扫源文，生成全书概览+逐章梗概注入翻译
  prescan_concurrency: 4 # 预扫逐章梗概的并发线程数（各章独立，1=串行）
  review_concurrency: 4 # 章末审校分块的并发线程数（只读固定译文/术语，1=串行）
  glossary_scope: chapter # chapter=本章相关词条；full=全量表

# ── 敬称策略（日语源文本时生效，其它语言通常不会用到）────────────────────
honorific:
  # keep_style: 体现语气（前辈/小X/X君…）; normalize: 按统一规则；drop: 省略
  strategy: keep_style

# ── 标点规范化（统一为简体中文大陆通用全角标点）────────────────────────────
punctuation:
  normalize: true

# ── 路径 ─────────────────────────────────────────────────────────────────
paths:
  state_dir: state # 运行状态、各章中间产物、术语库

# ── 双语输出 ───────────────────────────────────────────────────────────────
output:
  mono: true # 产出单语中文版（<书名>.zh.epub）
  bilingual: false # 产出原文与译文对照版（<书名>.zh-bi.epub）
  bilingual_order: target_first # target_first=译文在上；source_first=原文在上
  about_page: true # 在书末附加“关于此翻译”说明页
"""


class TierConfig(BaseModel):
    """跨 provider 通用的档位覆盖；专属参数由 provider 解析 options。"""

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class LLMConfig(BaseModel):
    provider: str = "deepseek"
    base_url: str | None = None
    api_key_env: str | None = None
    timeout: int = 600
    max_retries: int = 4
    tiers: dict[str, TierConfig] = Field(default_factory=dict)


class SegmentConfig(BaseModel):
    max_chars_per_batch: int = 1800
    max_chars_per_segment: int = 1200


class PipelineConfig(BaseModel):
    review: bool = True
    autofix_severe: bool = False     # 章末审校后自动重译严重项（漏译/误译）；关闭则仅上报留人工
    align_retry_limit: int = 2       # 批次翻译段数不符时的整批重试次数，超限后逐段兜底
    polish: bool = True              # 默认开：润色=用强档把全书再翻一遍，可在配置中关闭以节省成本
    backtranslate_sample: float = 0.0
    consistency_qa: bool = False
    rolling_context_segments: int = 6
    # 翻译前预扫源文，生成全书概览+逐章梗概注入翻译 prompt（让译者对全书有理解）。
    # fast 档（免思考），且全局概览为恒定前缀可命中缓存复用；关掉可省去预扫成本。
    book_understanding: bool = True
    prescan_concurrency: int = 4     # 预扫逐章梗概的并发线程数（各章独立，1=串行）
    review_concurrency: int = 4      # 章末审校分块并发数（结果按原块序合并，1=串行）
    glossary_scope: str = "chapter"  # chapter=只注入本章出现的词条+锁定人物（省 token）；full=全量表


class OutputConfig(BaseModel):
    mono: bool = True  # 产出单语版
    bilingual: bool = False  # 产出双语版
    bilingual_order: str = (
        "target_first"  # target_first=译文在上原文在下(默认); source_first=原文在上
    )
    about_page: bool = True  # 在书末附加项目说明页


class Config(BaseModel):
    source_lang: str = "auto"        # auto | ja | en | …（auto 时由模型检测）
    target_lang: str = "zh"
    llm: LLMConfig = Field(default_factory=LLMConfig)
    segment: SegmentConfig = Field(default_factory=SegmentConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    honorific_strategy: str = "keep_style"
    punctuation_normalize: bool = True  # 译文标点规范化为简体中文通用
    state_dir: str = "state"

    @staticmethod
    def create_default_file(path: str) -> bool:
        """在 path 不存在时原子创建默认配置，返回是否由本次创建。"""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8") as f:
                f.write(_DEFAULT_CONFIG_YAML)
            return True
        except FileExistsError:
            return False

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        cls.create_default_file(path)
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Config":
        lang = raw.get("language", {})
        llm_raw = raw.get("llm", {})
        tiers = {
            name: TierConfig.model_validate(t)
            for name, t in (llm_raw.get("tiers", {}) or {}).items()
        }
        llm = LLMConfig(
            provider=llm_raw.get("provider", "deepseek"),
            base_url=llm_raw.get("base_url"),
            api_key_env=llm_raw.get("api_key_env"),
            timeout=llm_raw.get("timeout", 600),
            max_retries=llm_raw.get("max_retries", 4),
            tiers=tiers,
        )
        segment = SegmentConfig.model_validate(raw.get("segment", {}) or {})
        pipeline = PipelineConfig.model_validate(raw.get("pipeline", {}) or {})
        output = OutputConfig.model_validate(raw.get("output", {}) or {})
        punct = raw.get("punctuation", {}) or {}
        return cls(
            source_lang=lang.get("source", "auto"),
            target_lang=lang.get("target", "zh"),
            llm=llm,
            segment=segment,
            pipeline=pipeline,
            output=output,
            honorific_strategy=raw.get("honorific", {}).get("strategy", "keep_style"),
            punctuation_normalize=bool(punct.get("normalize", True)),
            state_dir=raw.get("paths", {}).get("state_dir", "state"),
        )
