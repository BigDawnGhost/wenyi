"""输入文档解析和预处理的用户可见异常。"""


class IngestError(RuntimeError):
    """表示可向 CLI 用户直接展示的输入处理错误。"""


class MinerUError(IngestError):
    """表示 MinerU 请求、解析或结果处理失败。"""


class MinerUTimeoutError(MinerUError):
    """表示 MinerU 任务在限定时间内未完成。"""
