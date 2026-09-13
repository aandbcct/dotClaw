import { useEffect, useState } from "react";

import type { WorkspaceOptions } from "./types";

interface CreateSessionDialogProps {
  options: WorkspaceOptions;
  busy: boolean;
  onCancel: () => void;
  onCreate: (agentId: string, model: string) => void;
}

export function CreateSessionDialog({
  options,
  busy,
  onCancel,
  onCreate,
}: CreateSessionDialogProps) {
  const [agentId, setAgentId] = useState(options.default_agent_id);
  const [model, setModel] = useState(options.default_model);

  useEffect(() => {
    setAgentId(options.default_agent_id);
    setModel(options.default_model);
  }, [options]);

  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onCancel}>
      <section
        className="create-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-dialog-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-heading">
          <div>
            <p>NEW SESSION</p>
            <h2 id="create-dialog-title">选择这次对话的工作方式</h2>
          </div>
          <button type="button" aria-label="关闭新建会话" onClick={onCancel}>×</button>
        </div>

        <label className="dialog-field">
          <span>Agent</span>
          <select aria-label="Agent" value={agentId} onChange={(event) => setAgentId(event.target.value)}>
            {options.agents.map((agent) => (
              <option key={agent.id} value={agent.id}>{agent.name}</option>
            ))}
          </select>
          <small>{options.agents.find((agent) => agent.id === agentId)?.description || "使用该 Agent 的身份、工具与策略配置"}</small>
        </label>

        <label className="dialog-field">
          <span>模型</span>
          <select aria-label="模型" value={model} onChange={(event) => setModel(event.target.value)}>
            {options.models.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
          <small>模型选择会随会话保存，后续运行保持一致。</small>
        </label>

        <div className="dialog-actions">
          <button type="button" className="secondary-button" onClick={onCancel}>取消</button>
          <button
            type="button"
            className="primary-button"
            disabled={busy || !agentId || !model}
            onClick={() => onCreate(agentId, model)}
          >
            {busy ? "正在创建" : "开始对话"}
          </button>
        </div>
      </section>
    </div>
  );
}
