// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/** Choose an option by its value in a Combobox (the searchable picker), like selectOptions. */
export async function pick(trigger: HTMLElement, value: string) {
  await userEvent.click(trigger);
  const list = await screen.findByRole("listbox");
  const opt = list.querySelector<HTMLElement>(`[role=option][data-value="${value}"]`);
  if (!opt) throw new Error(`no option with value "${value}"`);
  await userEvent.click(opt);
}
