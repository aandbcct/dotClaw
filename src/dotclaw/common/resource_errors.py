"""基础能力报告资源尚未释放时使用的独立异常。"""


class ResourceCleanupError(RuntimeError):
    """资源清理失败，不能归一化成普通业务失败。"""

    def __init__(self, resource_kind: str, diagnostic_code: str, message: str) -> None:
        super().__init__(message)
        self.resource_kind = resource_kind
        self.diagnostic_code = diagnostic_code
