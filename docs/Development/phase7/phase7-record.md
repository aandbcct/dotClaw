# dotClaw Phase 7 Skill 系统完善 — 开发日志

> 本文件记录 P7 Skill 系统完善的开发进度、变更记录。
> 架构文档见 `docs/phase7/phase7-roadmap.md`。

## 变更日志

| 版本 | 日期 | 变更内容 |
|------|------|----------|
| v1.0 | 2026-06-05 | Phase 7 Skill 系统完善首次实施，4步全部完成，127/127 测试通过 |
| v1.1 | 2026-06-05 | 修复代码审查 W1（symlink 循环）+ M1-M5（CR 规范/截断重复/日志/ CJK/空描述），127/127 测试通过 |

### 变更内容

- 新增 `skills/models.py`：SkillMeta frozen dataclass（全量 frontmatter + 文件系统字段）+ SkillLifecycle 枚举（PERSISTENT/ONE_SHOT/EPHEMERAL）
- 修改 `config/settings.py`：SkillsConfig 扩展（directory 支持 str|list、新增 enabled/skip_prefix 字段）+ _raw_to_config 适配
- 新增 `skills/scanner.py`：SkillScanner — 递归扫描 SKILL.md、跳过 _ 前缀目录、yaml.safe_load 解析、无效 lifecycle 降级、scripts/references 子目录扫描、重名检测
- 新增 `skills/registry.py`：SkillRegistry — register/get/list_all CRUD + get_descriptions_block 生成 prompt 描述块
- 删除 `skills/loader.py`：旧 SkillLoader + Skill 类由 scanner + registry 替代
- 修改 `skills/__init__.py`：更新导出为 SkillMeta/SkillLifecycle/SkillScanner/SkillRegistry
- 修改 `agent/context.py`：新增 skill_registry 字段（TYPE_CHECKING 导入）
- 修改 `agent/prompt/providers.py`：SkillsProvider.provide() 实现 — 技能系统提示词（mandatory 说明）+ 可用技能列表
- 修改 `agent/loop.py`：AgentLoop 新增 skill_registry 参数 + _build_context() 传入
- 修改 `main.py`：Skill 初始化链（SkillsConfig.enabled → SkillScanner → SkillRegistry → AgentLoop）+ SkillsProvider 注册 + /skills 命令 + 帮助文本
- 新增 `tests/test_phase7_acceptance.py`：32 tests / 7 场景（Meta/Lifecycle/Config/Scanner/Registry/Provider/AgentContext/Regression）

### 回归测试结果

| 测试套件 | 测试数 | 通过 | 状态 |
|----------|--------|------|------|
| Phase 1 验收 | 7 | 7 | ✅ |
| Phase 2 验收 | 7 | 7 | ✅ |
| Phase 3 验收 | 8 | 8 | ✅ |
| Phase 4 验收 | 6 | 6 | ✅ |
| Phase 5 验收 | 39 | 39 | ✅ |
| Phase 6 验收 | 28 | 28 | ✅ |
| Phase 7 验收 | 32 | 32 | ✅ |
| **合计** | **127** | **127** | **✅** |

---

## v1.1 — 2026-06-05

### 变更内容

根据代码审查报告 `docs/phase7/phase7-codeReview.md` 修复 Warning 级别问题 W1 + Minor 级别问题 M1-M5。

### 已修复（来自审查 Warning + Minor）

| # | 原问题 | 修复内容 | 涉及文件 |
|---|--------|----------|----------|
| ✅ W1 | 目录遍历存在符号链接循环风险 | `_walk()` 使用 `entry.is_dir(follow_symlinks=False)` 禁用 symlink 跟随；`_scan_subdir()` 使用 `subdir.is_dir(follow_symlinks=False)` + `f.is_symlink()` 检查 | `skills/scanner.py` |
| ✅ M1 | CR/LF 规范化不完整（缺孤 \r） | `_parse_frontmatter()` 增加 `content.replace('\r', '\n')` 处理旧 Mac 格式 | `skills/scanner.py` |
| ✅ M2 | 描述截断逻辑重复（registry + main） | `SkillMeta` 新增 `truncated_description(max_len)` 共享方法；`SkillRegistry.get_descriptions_block()` 和 `_cmd_skills()` 均改用此方法 | `skills/models.py`, `skills/registry.py`, `main.py` |
| ✅ M3 | registry logger 定义但未使用 | `SkillRegistry.register()` 添加 debug 级别覆盖日志 | `skills/registry.py` |
| ✅ M4 | max_desc_len 过短（20字符不适合 CJK） | 默认值从 20 提升到 40（兼容中英文） | `skills/registry.py`, `main.py` |
| ✅ M5 | SkillMeta 无空 description 保护 | `_parse_skill()` 中 `description` 为空时添加 debug 级别 hint 日志 | `skills/scanner.py` |

---

*本文件由 dotClaw 开发工程师维护。Phase 7 审查修复完成。*
