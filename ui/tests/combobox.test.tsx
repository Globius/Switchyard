// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { Combobox } from "@/components/ui/combobox";

const OPTIONS = [
  { value: "", label: "None" },
  { value: "a", label: "alpha" },
  { value: "b", label: "bravo" },
  { value: "c", label: "charlie" },
];

function Harness() {
  const [v, setV] = useState("");
  return (
    <>
      <Combobox aria-label="Pool" value={v} onChange={setV} options={OPTIONS} />
      <output>{v}</output>
    </>
  );
}

describe("Combobox", () => {
  it("filters as you type and picks with Enter", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole("combobox", { name: "Pool" }));
    await userEvent.type(screen.getByRole("textbox", { name: "Search" }), "ra{Enter}");
    expect(screen.getByRole("combobox", { name: "Pool" })).toHaveTextContent("bravo");
    expect(screen.getByRole("status")).toHaveTextContent("b");
  });

  it("moves with the arrow keys, shows no matches, and picks with a click", async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole("combobox", { name: "Pool" }));
    const search = screen.getByRole("textbox", { name: "Search" });
    await userEvent.type(search, "zzz");
    expect(screen.getByText("No matches")).toBeInTheDocument();
    await userEvent.clear(search);
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{ArrowUp}{Enter}");
    expect(screen.getByRole("status")).toHaveTextContent("a");
    await userEvent.click(screen.getByRole("combobox", { name: "Pool" }));
    await userEvent.click(await screen.findByRole("option", { name: "charlie" }));
    expect(screen.getByRole("status")).toHaveTextContent("c");
  });
});
