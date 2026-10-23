import { useState } from "react";
import { Dialog } from "./Dialog";
import type { WorkspaceOptions } from "./types";

/** 高级选择只设置下一次首发参数，不提前创建空会话。 */
export function CreateSessionDialog({ options, onCancel, onCreate }: { options: WorkspaceOptions; onCancel: () => void; onCreate: (agentId: string, model: string) => void }) {
  const [agentId, setAgentId] = useState(options.default_agent_id);
  const [model, setModel] = useState(options.default_model);
  return <Dialog title="选择这次对话的工作方式" className="create-dialog" onClose={onCancel}>
    <label className="dialog-field">智能体<select aria-label="Agent" value={agentId} onChange={(event) => setAgentId(event.target.value)}>{options.agents.map((agent) => <option value={agent.id} key={agent.id}>{agent.name}</option>)}</select><small>{options.agents.find((agent) => agent.id === agentId)?.description}</small></label>
    <label className="dialog-field">模型<select aria-label="模型" value={model} onChange={(event) => setModel(event.target.value)}>{options.models.map((name) => <option key={name}>{name}</option>)}</select><small>用于后续新消息</small></label>
    <footer className="dialog-actions"><button type="button" className="secondary-button" onClick={onCancel}>取消</button><button type="button" className="primary-button" disabled={!agentId || !model} onClick={() => onCreate(agentId, model)}>使用此选择</button></footer>
  </Dialog>;
}
