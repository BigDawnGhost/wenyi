import { PageContainer, PageHeader } from "../../components/layout/AppLayout";
import { useI18n } from "../../i18n";
import { GlobalConfiguration } from "./GlobalConfiguration";

export default function InterfaceSettingsPage() {
  const { t } = useI18n();

  return (
    <>
      <PageHeader title={t("settings.title")} />
      <PageContainer className="max-w-5xl">
        <GlobalConfiguration />
      </PageContainer>
    </>
  );
}
