import { useEffect } from "react";
import { useI18n } from "./index";

export function DocumentLanguage() {
  const { locale, t } = useI18n();
  useEffect(() => {
    document.documentElement.lang = locale;
    document.title = t("app.documentTitle");
  }, [locale, t]);
  return null;
}
