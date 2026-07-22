"""内置工具子包——可信工具包。

Tool v1 阶段二起不再有手工 register_all()：所有工具通过 @tool 声明，由
ToolDiscovery 扫描本包及其子模块自动发现（见 dotclaw.tools.discovery）。
新增工具只需在子模块中用 @tool 装饰函数，无需修改任何注册列表。
"""
