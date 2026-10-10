import { t } from "@/lib/i18n";

/** Placeholder for screens that arrive in later MRs of the U1 build. */
export function NotBuilt({ name }: { name: string }) {
  return (
    <div className="p-8">
      <h1 className="text-2xl font-semibold">{name}</h1>
      <p className="mt-2 text-sm text-muted-foreground">{t("This screen is not built yet.")}</p>
    </div>
  );
}
