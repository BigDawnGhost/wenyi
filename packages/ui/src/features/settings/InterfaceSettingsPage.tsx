import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { useI18n } from "@/i18n";
import { GlobalConfiguration } from "./GlobalConfiguration";
import { LanguageSettings } from "./LanguageSettings";

export default function InterfaceSettingsPage() {
  const { t } = useI18n();
  return (
    <>
      <PageHeader
        title={t("settings.title")}
        subtitle={t("settings.subtitle")}
      />
      <PageContainer className="max-w-5xl space-y-4">
        <LanguageSettings />
        <GlobalConfiguration />
      </PageContainer>
    </>
  );
}
