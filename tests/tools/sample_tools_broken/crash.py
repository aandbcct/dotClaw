"""故意在导入时抛出异常的子模块，用于验证 Discovery 的导入失败记录。"""

raise RuntimeError("模拟子模块导入失败")
