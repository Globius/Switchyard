// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

/**
 * A secret the server shows only once (API token, enrollment token, break-glass password and
 * authenticator secret). Each value has a Copy button, the authenticator setup can show a QR code,
 * and a checkbox "I have stored it" gates the caller's Done button.
 *
 * @example
 * const [stored, setStored] = useState(false);
 * <ShowOnceSecret secrets={[{ label: "Token", value: token }]} stored={stored} onStoredChange={setStored} />
 * <Button disabled={!stored}>Done</Button>
 */
import { Check, Copy } from "lucide-react";
import QRCode from "qrcode";
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { t } from "@/lib/i18n";

export interface Secret {
  label: string;
  value: string;
}

function CopyButton({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      size="sm"
      variant="outline"
      aria-label={t("Copy {label}", { label })}
      onClick={() => {
        void navigator.clipboard?.writeText(value).then(() => setCopied(true));
      }}
    >
      {copied ? <Check /> : <Copy />} {copied ? t("Copied") : t("Copy")}
    </Button>
  );
}

/** QR code of an `otpauth://` URI, drawn as inline SVG so no image request leaks the secret. */
export function Qr({ uri, label }: { uri: string; label: string }) {
  const [svg, setSvg] = useState("");
  useEffect(() => {
    let off = false;
    void QRCode.toString(uri, { type: "svg", margin: 1, width: 168 }).then((s) => {
      if (!off) setSvg(s);
    });
    return () => {
      off = true;
    };
  }, [uri]);
  if (!svg) return null;
  return (
    <div
      role="img"
      aria-label={label}
      className="w-fit rounded-md border bg-white p-1"
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}

export function ShowOnceSecret({
  secrets,
  qrUri,
  stored,
  onStoredChange,
  note,
}: {
  secrets: Secret[];
  /** Authenticator setup URI; shown as a QR code next to the secrets. */
  qrUri?: string;
  stored: boolean;
  onStoredChange: (v: boolean) => void;
  note?: string;
}) {
  return (
    <div className="space-y-3" role="group" aria-label={t("Shown once")}>
      <p className="text-sm text-muted-foreground">
        {note ?? t("Copy it now. It is not shown again.")}
      </p>
      {secrets.map((s) => (
        <div key={s.label} className="space-y-1">
          <div className="text-xs font-medium text-muted-foreground">{s.label}</div>
          <div className="flex items-start gap-2">
            <code className="min-w-0 flex-1 rounded bg-muted p-2 font-mono text-xs break-all">
              {s.value}
            </code>
            <CopyButton value={s.value} label={s.label} />
          </div>
        </div>
      ))}
      {qrUri && <Qr uri={qrUri} label={t("Authenticator QR code")} />}
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={stored}
          onChange={(e) => onStoredChange(e.target.checked)}
        />
        {t("I have stored it")}
      </label>
    </div>
  );
}
