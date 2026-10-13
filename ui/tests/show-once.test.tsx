// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import { ShowOnceSecret } from "@/components/sy/ShowOnceSecret";

function Harness({ qr }: { qr?: string }) {
  const [stored, setStored] = useState(false);
  return (
    <>
      <ShowOnceSecret
        secrets={[
          { label: "Token", value: "sy_abc" },
          { label: "Password", value: "pw" },
        ]}
        qrUri={qr}
        stored={stored}
        onStoredChange={setStored}
      />
      <button disabled={!stored}>Done</button>
    </>
  );
}

describe("ShowOnceSecret", () => {
  it("copies a value and gates Done on the stored checkbox", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    render(<Harness />);
    expect(screen.getByText("sy_abc")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Copy Token" }));
    expect(writeText).toHaveBeenCalledWith("sy_abc");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Done" })).toBeDisabled();
    await userEvent.click(screen.getByLabelText("I have stored it"));
    expect(screen.getByRole("button", { name: "Done" })).toBeEnabled();
  });

  it("draws a QR code for an authenticator URI as inline SVG", async () => {
    render(<Harness qr="otpauth://totp/Switchyard:breakglass?secret=ABC&issuer=Switchyard" />);
    const qr = await screen.findByRole("img", { name: "Authenticator QR code" });
    await waitFor(() => expect(qr.querySelector("svg")).not.toBeNull());
  });
});
