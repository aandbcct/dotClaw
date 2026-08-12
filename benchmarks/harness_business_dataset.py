"""PR8 Agent Harness 业务 Dataset 的版本化加载与严格资格校验。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence


class HarnessDatasetError(ValueError):
    """Dataset Manifest 或任务实例不满足冻结契约。"""


class TaskFamily(StrEnum):
    """正式业务任务族。"""

    EVIDENCE_RESEARCH = "evidence_research"
    WORKSPACE_ENGINEERING = "workspace_engineering"
    TOOL_APPROVAL_WORKFLOW = "tool_approval_workflow"
    LONG_CONTEXT_CONTINUITY = "long_context_continuity"
    MULTI_AGENT = "multi_agent"
    MIXED_COMPLEX = "mixed_complex"


class Difficulty(StrEnum):
    """任务实例难度。"""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class QualityDimension(StrEnum):
    """开放式交付的原子质量维度。"""

    GROUNDEDNESS = "groundedness"
    CONSTRAINT_COMPLIANCE = "constraint_compliance"
    COMPLETENESS = "completeness"
    ACTIONABILITY = "actionability"


class ExecutionCondition(StrEnum):
    """Full Harness 与六类匹配基线。"""

    FULL = "full"
    SINGLE_PASS_RESEARCH = "single_pass_research"
    SINGLE_AGENT_WORKSPACE = "single_agent_workspace"
    DIRECT_TOOL_WITHOUT_RESUME = "direct_tool_without_resume"
    RECENT_WINDOW_ONLY = "recent_window_only"
    SINGLE_AGENT_NO_DELEGATION = "single_agent_no_delegation"
    PRIMARY_CAPABILITY_REMOVED = "primary_capability_removed"


@dataclass(frozen=True)
class AtomicJudgeCriterion:
    """一个只能独立通过或失败的开放式交付判据。"""

    criterion_id: str
    description: str
    dimension: QualityDimension
    required: bool


@dataclass(frozen=True)
class HarnessTaskInstance:
    """Git 跟踪的单个业务任务实例。"""

    instance_id: str
    family: TaskFamily
    difficulty: Difficulty
    title: str
    user_task: str
    expected_delivery: str
    allowed_facts: tuple[str, ...]
    distractors: tuple[str, ...]
    required_constraints: tuple[str, ...]
    complexity_factors: tuple[str, ...]
    deterministic_checks: tuple[str, ...]
    capability_tags: tuple[str, ...]
    criteria: tuple[AtomicJudgeCriterion, ...]

    @property
    def criterion_dimensions(self) -> Mapping[str, str]:
        """提供适合写入单样本的判据维度映射。"""
        return {item.criterion_id: item.dimension.value for item in self.criteria}


@dataclass(frozen=True)
class FamilyDefinition:
    """任务族及其唯一主要匹配基线。"""

    family: TaskFamily
    baseline: ExecutionCondition
    instance_file: str


@dataclass(frozen=True)
class HarnessDataset:
    """经完整校验的业务 Dataset 只读视图。"""

    dataset_id: str
    version: str
    formal_repeat: int
    preflight_per_condition: int
    families: tuple[FamilyDefinition, ...]
    instances: tuple[HarnessTaskInstance, ...]
    content_hash: str

    def baseline_for(self, family: TaskFamily) -> ExecutionCondition:
        """返回任务族唯一匹配基线。"""
        return next(item.baseline for item in self.families if item.family is family)


_EXPECTED_DIFFICULTIES = {Difficulty.EASY: 12, Difficulty.MEDIUM: 30, Difficulty.HARD: 18}
_UNSUPPORTED_CLAIM_CRITERION = AtomicJudgeCriterion(
    "unsupported_claim_absent",
    "候选未引入 allowed_facts 之外的事实性断言",
    QualityDimension.GROUNDEDNESS,
    True,
)
_BASELINES = {
    TaskFamily.EVIDENCE_RESEARCH: ExecutionCondition.SINGLE_PASS_RESEARCH,
    TaskFamily.WORKSPACE_ENGINEERING: ExecutionCondition.SINGLE_AGENT_WORKSPACE,
    TaskFamily.TOOL_APPROVAL_WORKFLOW: ExecutionCondition.DIRECT_TOOL_WITHOUT_RESUME,
    TaskFamily.LONG_CONTEXT_CONTINUITY: ExecutionCondition.RECENT_WINDOW_ONLY,
    TaskFamily.MULTI_AGENT: ExecutionCondition.SINGLE_AGENT_NO_DELEGATION,
    TaskFamily.MIXED_COMPLEX: ExecutionCondition.PRIMARY_CAPABILITY_REMOVED,
}


def load_harness_dataset(root: Path, dataset_id: str = "agent_harness_business_v1") -> HarnessDataset:
    """读取 Manifest 与六个任务族文件，并验证完整正式口径。"""
    base = root / dataset_id
    manifest = _read_object(base / "manifest.json")
    if manifest.get("dataset_id") != dataset_id or manifest.get("version") != "1":
        raise HarnessDatasetError("Dataset 标识或版本不符合 agent_harness_business_v1 契约")
    if manifest.get("formal_repeat") != 3 or manifest.get("preflight_per_condition") != 1:
        raise HarnessDatasetError("正式重复次数必须为 3，且每执行条件仅允许一次 preflight")
    raw_families = manifest.get("families")
    if not isinstance(raw_families, list) or len(raw_families) != len(TaskFamily):
        raise HarnessDatasetError("Manifest 必须精确声明六个任务族")

    families = tuple(_parse_family(item) for item in raw_families)
    if {item.family for item in families} != set(TaskFamily):
        raise HarnessDatasetError("Manifest 任务族必须唯一且完整")
    if any(_BASELINES[item.family] is not item.baseline for item in families):
        raise HarnessDatasetError("任务族主要 Baseline 与冻结设计不一致")

    instances: list[HarnessTaskInstance] = []
    hash_payload: list[object] = [manifest]
    for family in families:
        raw = _read_array(base / "instances" / family.instance_file)
        hash_payload.append(raw)
        parsed = [_parse_instance(item, family.family) for item in raw]
        if len(parsed) != 10:
            raise HarnessDatasetError(f"任务族 {family.family.value} 必须精确包含 10 个实例")
        family_distribution = {level: sum(item.difficulty is level for item in parsed) for level in Difficulty}
        if family_distribution != {Difficulty.EASY: 2, Difficulty.MEDIUM: 5, Difficulty.HARD: 3}:
            raise HarnessDatasetError(f"任务族 {family.family.value} 难度必须为 2/5/3")
        instances.extend(parsed)

    ids = [item.instance_id for item in instances]
    if len(instances) != 60 or len(ids) != len(set(ids)):
        raise HarnessDatasetError("Dataset 必须包含 60 个全局唯一实例")
    distribution = {level: sum(item.difficulty is level for item in instances) for level in Difficulty}
    if distribution != _EXPECTED_DIFFICULTIES:
        raise HarnessDatasetError("Dataset 难度分布必须为 Easy 12、Medium 30、Hard 18")
    semantic_keys = {(item.user_task, item.allowed_facts) for item in instances}
    if len(semantic_keys) != len(instances):
        raise HarnessDatasetError("任务实例不得复用相同用户任务与事实集合")
    content = json.dumps(hash_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return HarnessDataset(dataset_id, "1", 3, 1, families, tuple(instances), hashlib.sha256(content.encode("utf-8")).hexdigest())


def _parse_family(value: object) -> FamilyDefinition:
    """解析单个任务族声明。"""
    if not isinstance(value, dict) or set(value) != {"family", "baseline", "instance_file"}:
        raise HarnessDatasetError("任务族声明字段非法")
    try:
        family = TaskFamily(_required_text(value, "family"))
        baseline = ExecutionCondition(_required_text(value, "baseline"))
    except ValueError as error:
        raise HarnessDatasetError("任务族或 Baseline 枚举非法") from error
    instance_file = _required_text(value, "instance_file")
    if Path(instance_file).name != instance_file or not instance_file.endswith(".json"):
        raise HarnessDatasetError("任务族实例文件必须是 instances 目录内的 JSON 文件名")
    return FamilyDefinition(family, baseline, instance_file)


def _parse_instance(value: object, expected_family: TaskFamily) -> HarnessTaskInstance:
    """解析并校验一个任务实例及其原子判据。"""
    if not isinstance(value, dict):
        raise HarnessDatasetError("任务实例必须为 JSON 对象")
    try:
        family = TaskFamily(_required_text(value, "family"))
        difficulty = Difficulty(_required_text(value, "difficulty"))
    except ValueError as error:
        raise HarnessDatasetError("任务实例 family 或 difficulty 非法") from error
    if family is not expected_family:
        raise HarnessDatasetError("任务实例所属任务族与文件声明不一致")
    sequence_fields = {
        key: _required_text_list(value, key)
        for key in ("allowed_facts", "distractors", "required_constraints", "complexity_factors", "deterministic_checks", "capability_tags")
    }
    if len(sequence_fields["allowed_facts"]) < 3:
        raise HarnessDatasetError("每个实例至少需要三条允许事实")
    if difficulty is Difficulty.HARD and len(set(sequence_fields["complexity_factors"])) < 2:
        raise HarnessDatasetError("Hard 实例至少需要两种复杂因素")
    if difficulty is Difficulty.MEDIUM and not sequence_fields["complexity_factors"]:
        raise HarnessDatasetError("Medium 实例至少需要一种复杂因素")
    raw_criteria = value.get("criteria")
    if not isinstance(raw_criteria, list) or not raw_criteria:
        raise HarnessDatasetError("每个实例必须定义原子 Judge 判据")
    criteria = tuple(_parse_criterion(item) for item in raw_criteria)
    criterion_ids = [item.criterion_id for item in criteria]
    if len(criterion_ids) != len(set(criterion_ids)) or not any(item.required for item in criteria):
        raise HarnessDatasetError("Judge 判据 ID 必须唯一且至少包含一个必需判据")
    if _UNSUPPORTED_CLAIM_CRITERION.criterion_id in criterion_ids:
        raise HarnessDatasetError("实例不得覆盖 Dataset 统一的事实越界判据")
    criteria = criteria + (_UNSUPPORTED_CLAIM_CRITERION,)
    return HarnessTaskInstance(
        instance_id=_required_text(value, "instance_id"),
        family=family,
        difficulty=difficulty,
        title=_required_text(value, "title"),
        user_task=_required_text(value, "user_task"),
        expected_delivery=_required_text(value, "expected_delivery"),
        allowed_facts=sequence_fields["allowed_facts"],
        distractors=sequence_fields["distractors"],
        required_constraints=sequence_fields["required_constraints"],
        complexity_factors=sequence_fields["complexity_factors"],
        deterministic_checks=sequence_fields["deterministic_checks"],
        capability_tags=sequence_fields["capability_tags"],
        criteria=criteria,
    )


def _parse_criterion(value: object) -> AtomicJudgeCriterion:
    """解析单个原子质量判据。"""
    if not isinstance(value, dict) or set(value) != {"id", "description", "dimension", "required"}:
        raise HarnessDatasetError("原子 Judge 判据字段非法")
    try:
        dimension = QualityDimension(_required_text(value, "dimension"))
    except ValueError as error:
        raise HarnessDatasetError("Judge 质量维度非法") from error
    required = value["required"]
    if not isinstance(required, bool):
        raise HarnessDatasetError("Judge 判据 required 必须为布尔值")
    return AtomicJudgeCriterion(_required_text(value, "id"), _required_text(value, "description"), dimension, required)


def _required_text(value: Mapping[str, object], key: str) -> str:
    """读取必需非空文本。"""
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise HarnessDatasetError(f"{key} 必须为非空字符串")
    return item


def _required_text_list(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    """读取必需非空字符串数组。"""
    item = value.get(key)
    if not isinstance(item, list) or not item or not all(isinstance(part, str) and part for part in item):
        raise HarnessDatasetError(f"{key} 必须为非空字符串数组")
    return tuple(item)


def _read_object(path: Path) -> Mapping[str, object]:
    """读取 JSON 对象并统一损坏错误。"""
    value = _read_json(path)
    if not isinstance(value, dict):
        raise HarnessDatasetError(f"{path} 必须是 JSON 对象")
    return value


def _read_array(path: Path) -> Sequence[object]:
    """读取 JSON 数组并统一损坏错误。"""
    value = _read_json(path)
    if not isinstance(value, list):
        raise HarnessDatasetError(f"{path} 必须是 JSON 数组")
    return value


def _read_json(path: Path) -> object:
    """读取 Git 跟踪的 UTF-8 JSON。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HarnessDatasetError(f"无法读取 Dataset 文件：{path}") from error
