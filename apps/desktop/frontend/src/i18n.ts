import { useI18n } from "@wenyi/ui/i18n";

const en = {
  "runtime.starting": "Starting local service…",
  "runtime.failed": "Local service unavailable",
  "runtime.closing": "Local service is closing…",
  "runtime.reload": "Reload",
  "credentials.title": "Desktop connection credential",
  "credentials.environment": "Environment variable",
  "credentials.environmentHelp": "Read {name} from the Desktop backend environment. Set the variable name above and save configuration first.",
  "credentials.key": "API key",
  "credentials.noEcho": "Leave blank to keep the saved key",
  "credentials.system": "System credential store",
  "credentials.session": "This session only",
  "credentials.autoHelp": "Keys are saved to the system credential store when possible, otherwise only for this session. Saved keys are never displayed.",
  "credentials.advanced": "Advanced: environment variable",
  "credentials.useEnvironment": "Clear manual key and use environment variable",
  "credentials.sessionHelp": "Saved for this session only. Enter the key again next time you start Desktop.",
  "credentials.available": "Saved source: credential available",
  "credentials.missing": "Saved source: no credential configured",
  "credentials.notRequired": "This provider does not require a credential",
  "credentials.offline": "Checks local availability only, not provider connectivity or key validity. No model request is sent.",
  "credentials.failed": "Credential could not be saved. Please retry.",
  "credentials.save": "Save key",
  "credentials.clear": "Clear manual key",
  "credentials.refresh": "Check local availability",
  "credentials.saveConnection": "Save the connection configuration first, then configure its credential.",
};
const zhCN: Record<keyof typeof en, string> = {
  "runtime.starting": "正在启动本地服务…",
  "runtime.failed": "本地服务不可用",
  "runtime.closing": "本地服务正在关闭…",
  "runtime.reload": "重新加载",
  "credentials.title": "桌面端连接凭据",
  "credentials.environment": "环境变量",
  "credentials.environmentHelp": "从桌面后端进程环境读取 {name}。请在上方填写变量名并先保存配置。",
  "credentials.key": "API 密钥",
  "credentials.noEcho": "留空保留已保存的密钥",
  "credentials.system": "系统凭据库",
  "credentials.session": "仅本次会话",
  "credentials.autoHelp": "优先保存到系统凭据库；不可用时仅保留在本次会话。已保存的密钥不会回显。",
  "credentials.advanced": "高级：环境变量",
  "credentials.useEnvironment": "清除手动密钥并改用环境变量",
  "credentials.sessionHelp": "仅保存于本次会话，下次启动桌面端需重新输入密钥。",
  "credentials.available": "已保存来源：凭据可用",
  "credentials.missing": "已保存来源：尚未配置凭据",
  "credentials.notRequired": "此提供商无需凭据",
  "credentials.offline": "仅检查本地配置可用性，不验证真实联网或密钥有效性；不会发送模型请求。",
  "credentials.failed": "凭据保存失败，请重试。",
  "credentials.save": "保存密钥",
  "credentials.clear": "清除手动密钥",
  "credentials.refresh": "检查本地可用性",
  "credentials.saveConnection": "请先保存连接配置，再设置该连接的凭据。",
};

export function useDesktopI18n() {
  const { locale } = useI18n();
  return {
    t: (key: keyof typeof en, values: Record<string, unknown> = {}) =>
      (locale === "zh-CN" ? zhCN : en)[key].replace(
        /\{(\w+)\}/g, (_, name: string) => String(values[name] ?? ""),
      ),
  };
}
