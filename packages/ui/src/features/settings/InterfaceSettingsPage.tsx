import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { useI18n } from "@/i18n";
import { Navigate, useLocation, useParams } from "react-router-dom";
import { availableSettingsSections } from "@/routes/manifest";
import { GlobalConfiguration } from "./GlobalConfiguration";

export default function InterfaceSettingsPage() {
  const { t, locale } = useI18n();
  const { section = "general" } = useParams();
  const location = useLocation();
  const active = availableSettingsSections(locale, t).find((entry) => entry.id === section);

  if (!active || location.pathname === "/settings/general") {
    return <Navigate to="/settings" replace state={location.state} />;
  }

  return (
    <div className="flex min-h-dvh flex-col">
      <PageHeader title={active.label} />
      {/* The page heading replaces the title column, including in platform-supplied sections. */}
      <PageContainer className="flex w-full flex-1 flex-col [&_[data-slot='settings.section']]:block [&_[data-slot='settings.section']>h2]:hidden">
        <GlobalConfiguration section={active.id} />
      </PageContainer>
    </div>
  );
}
