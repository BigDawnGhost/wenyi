import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { useI18n } from "@/i18n";
import { GlobalConfiguration } from "./GlobalConfiguration";
import { platform } from "@/platform";

export default function InterfaceSettingsPage() {
  const { t } = useI18n();
  const Diagnostics = platform().capabilities.diagnostics?.Section;

  return (
    <>
      <PageHeader title={t("settings.title")} />
      <PageContainer className="max-w-5xl">
        <GlobalConfiguration />
        {Diagnostics && <Diagnostics />}
      </PageContainer>
    </>
  );
}
