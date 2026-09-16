import { Input, Label, Select } from "@/components/ui/form";
import { Button } from "@/components/ui/button";

type Document = Record<string, unknown>;
const object = (value: unknown) => (value || {}) as Document;

export function ProviderSettings({ config, disabled, kinds, onChange }: {
  config: Document; disabled: boolean; kinds: string[]; onChange: (llm: Document) => void;
}) {
  const llm = object(config.llm);
  const providers = object(llm.providers);
  const models = object(llm.models);
  const tiers = object(llm.tiers);
  const update = (group: string, id: string, patch: Document) => onChange({
    ...llm, [group]: { ...object(llm[group]), [id]: { ...object(object(llm[group])[id]), ...patch } },
  });
  const add = (group: string, prefix: string, value: Document) => {
    const entries = object(llm[group]);
    let i = 1;
    while (`${prefix}${i}` in entries) i++;
    onChange({ ...llm, [group]: { ...entries, [`${prefix}${i}`]: value } });
  };
  return <fieldset disabled={disabled} className="space-y-5 disabled:opacity-60">
    <div>
      <h2 className="font-medium">API 供应商与模型</h2>
      <p className="mt-2 text-sm text-muted-foreground">按项目保存。API Key 填写服务器已配置的环境变量名称（例如 DEEPSEEK_API_KEY），不要填写密钥本身。保存后，新的翻译任务和恢复任务使用新配置。</p>
    </div>
    {Object.entries(providers).map(([id, raw]) => {
      const provider = object(raw);
      return <div key={id} className="rounded-lg border p-4 space-y-3">
        <h3 className="font-medium text-sm">连接：{id}</h3>
        <div className="grid sm:grid-cols-2 gap-3">
          <div><Label htmlFor={`provider-${id}`}>API 供应商</Label><Select id={`provider-${id}`} value={String(provider.kind)} onChange={e => {
            // Protocol-specific options cannot be carried to another adapter.
            onChange({ ...llm, preset: null, providers: { ...providers, [id]: { kind: e.target.value, base_url: null, api_key_env: null } }, models: Object.fromEntries(Object.entries(models).map(([key, value]) => [key, object(value).provider === id ? { ...object(value), options: {} } : value])) });
          }}><option value={String(provider.kind)}>{String(provider.kind)}</option>{kinds.filter(k => k !== provider.kind && k !== "fake").map(k => <option key={k}>{k}</option>)}</Select></div>
          <div><Label htmlFor={`url-${id}`}>API 地址（Base URL）</Label><Input id={`url-${id}`} placeholder="留空使用供应商默认地址" value={String(provider.base_url || "")} onChange={e => update("providers", id, { base_url: e.target.value || null })} /></div>
          <div><Label htmlFor={`key-${id}`}>API Key 环境变量</Label><Input id={`key-${id}`} autoComplete="off" placeholder="留空使用供应商默认变量" value={String(provider.api_key_env || "")} onChange={e => update("providers", id, { api_key_env: e.target.value || null })} /></div>
          <div><Label htmlFor={`timeout-${id}`}>请求超时（秒）</Label><Input id={`timeout-${id}`} type="number" min={1} value={Number(provider.timeout || 600)} onChange={e => update("providers", id, { timeout: Number(e.target.value) })} /></div>
        </div>
      </div>;
    })}
    <Button type="button" variant="outline" onClick={() => add("providers", "provider", { kind: "openai-compatible" })}>添加 API 连接</Button>
    <p className="text-sm text-muted-foreground">切换供应商后，请将下面的模型名称改为该供应商支持的模型。模型 ID 用于档位和操作路由，模型名称用于实际 API 请求。</p>
    {Object.entries(models).map(([id, raw]) => {
      const model = object(raw);
      return <div key={id} className="grid sm:grid-cols-3 gap-3 rounded border p-3">
        <div className="text-sm self-center font-medium">模型 ID：{id}</div>
        <div><Label htmlFor={`connection-${id}`}>所属 API 连接</Label><Select id={`connection-${id}`} value={String(model.provider)} onChange={e => update("models", id, { provider: e.target.value, options: {} })}>{Object.keys(providers).map(p => <option key={p}>{p}</option>)}</Select></div>
        <div><Label htmlFor={`model-${id}`}>模型名称</Label><Input id={`model-${id}`} value={String(model.model || "")} onChange={e => update("models", id, { model: e.target.value })} /></div>
      </div>;
    })}
    <Button type="button" variant="outline" onClick={() => add("models", "model", { provider: Object.keys(providers)[0], model: "", options: {} })}>添加模型</Button>
    <div className="grid sm:grid-cols-3 gap-3">{[["strong", "高质量档位"], ["cheap", "经济档位"], ["fast", "快速档位"]].map(([tier, label]) => <div key={tier}><Label htmlFor={`tier-${tier}`}>{label}</Label><Select id={`tier-${tier}`} value={String(tiers[tier] || "")} onChange={e => onChange({ ...llm, tiers: { ...tiers, [tier]: e.target.value } })}>{Object.keys(models).map(m => <option key={m}>{m}</option>)}</Select></div>)}</div>
    <p className="text-xs text-muted-foreground">单独指定模型的操作路由优先于档位。可在下方“已保存配置的模型路由”中核对；复杂路由和备用模型继续通过高级 YAML 调整。</p>
  </fieldset>;
}
