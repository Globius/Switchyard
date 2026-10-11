import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { ChipInput, validateHostname, type ChipOption } from "@/components/sy/ChipInput";

const OPTIONS: ChipOption[] = [
  { value: "c1", label: "wildcard-example-com", hint: "12 days" },
  { value: "c2", label: "portal-example-com", hint: "41 days" },
  { value: "c3", label: "sso-example-com" },
];

function Harness(props: {
  initial?: string[];
  options?: ChipOption[];
  free?: boolean;
  itemErrors?: Record<string, string>;
  error?: string;
  onValue?: (v: string[]) => void;
  disabled?: boolean;
}) {
  const [v, setV] = useState<string[]>(props.initial ?? []);
  return (
    <>
      <ChipInput
        label="Certificates"
        value={v}
        onChange={(n) => {
          setV(n);
          props.onValue?.(n);
        }}
        options={props.options}
        freeText={props.free ? { validate: validateHostname } : undefined}
        itemErrors={props.itemErrors}
        error={props.error}
        disabled={props.disabled}
        emptyMessage={(q) => `No certificate named “${q}”.`}
      />
      <output data-testid="value">{v.join("|")}</output>
    </>
  );
}
const box = () => screen.getByRole("combobox", { name: "Certificates" });
const value = () => screen.getByTestId("value").textContent;

describe("ChipInput", () => {
  it("filters the list as you type and adds the highlighted item with Enter", async () => {
    render(<Harness options={OPTIONS} />);
    await userEvent.click(box());
    expect(screen.getAllByRole("option")).toHaveLength(3);
    await userEvent.type(box(), "port");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByText("41 days")).toBeInTheDocument();
    await userEvent.keyboard("{Enter}");
    expect(value()).toBe("c2");
    expect(screen.getByRole("group", { name: "portal-example-com" })).toBeInTheDocument();
    expect(box()).toHaveValue("");
  });

  it("moves with the arrow keys and adds with Tab; added items leave the list", async () => {
    render(<Harness options={OPTIONS} />);
    await userEvent.click(box());
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{Tab}");
    expect(value()).toBe("c3");
    expect(screen.queryByRole("option", { name: /sso-example-com/ })).not.toBeInTheDocument();
    await userEvent.keyboard("{ArrowUp}{Enter}");
    expect(value()).toBe("c3|c1");
  });

  it("shows the empty state for no matches and closes with Escape", async () => {
    render(<Harness options={OPTIONS} />);
    await userEvent.type(box(), "zzz");
    expect(screen.getByText("No certificate named “zzz”.")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("clicking an option adds it", async () => {
    render(<Harness options={OPTIONS} />);
    await userEvent.click(box());
    await userEvent.click(screen.getByRole("option", { name: /wildcard/ }));
    expect(value()).toBe("c1");
  });

  it("Backspace selects the last chip, again removes it; arrows move between chips; Delete removes the focused chip", async () => {
    render(<Harness options={OPTIONS} initial={["c1", "c2", "c3"]} />);
    await userEvent.click(box());
    await userEvent.keyboard("{Backspace}");
    expect(screen.getByRole("group", { name: "sso-example-com" })).toHaveFocus();
    expect(value()).toBe("c1|c2|c3");
    await userEvent.keyboard("{Backspace}");
    expect(value()).toBe("c1|c2");
    expect(screen.getByRole("group", { name: "portal-example-com" })).toHaveFocus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("group", { name: "wildcard-example-com" })).toHaveFocus();
    await userEvent.keyboard("{ArrowRight}{ArrowRight}");
    expect(box()).toHaveFocus();
    await userEvent.keyboard("{ArrowLeft}");
    expect(screen.getByRole("group", { name: "portal-example-com" })).toHaveFocus();
    await userEvent.keyboard("{Delete}");
    expect(value()).toBe("c1");
    await userEvent.keyboard("{Delete}");
    expect(value()).toBe("");
    expect(box()).toHaveFocus();
  });

  it("the × button removes a chip", async () => {
    render(<Harness options={OPTIONS} initial={["c1", "c2"]} />);
    await userEvent.click(screen.getByRole("button", { name: "Remove wildcard-example-com" }));
    expect(value()).toBe("c2");
  });

  it("free text: Enter or comma adds a valid hostname and an invalid one marks the box", async () => {
    render(<Harness free />);
    await userEvent.type(box(), "docs.example.com,");
    expect(value()).toBe("docs.example.com");
    await userEvent.type(box(), "*.example.com{Enter}");
    expect(value()).toBe("docs.example.com|*.example.com");
    await userEvent.type(box(), "not a host{Enter}");
    expect(
      await screen.findByText("Enter a hostname such as docs.example.com."),
    ).toBeInTheDocument();
    expect(box()).toHaveAttribute("aria-invalid", "true");
    expect(value()).toBe("docs.example.com|*.example.com");
  });

  it("an error on one item marks that chip; an error on the field marks the box", () => {
    render(
      <Harness
        options={OPTIONS}
        initial={["c1", "c2"]}
        itemErrors={{ c1: "expired on Sep 30" }}
        error="Choose at least one."
      />,
    );
    expect(
      screen.getByRole("group", { name: "wildcard-example-com: expired on Sep 30" }),
    ).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("wildcard-example-com: expired on Sep 30")).toBeInTheDocument();
    expect(screen.getByText("Choose at least one.")).toBeInTheDocument();
    expect(box()).toHaveAttribute("aria-invalid", "true");
  });

  it("disabled: cannot add or remove", async () => {
    render(<Harness options={OPTIONS} initial={["c1"]} disabled />);
    expect(box()).toBeDisabled();
    expect(screen.getByRole("button", { name: "Remove wildcard-example-com" })).toBeDisabled();
  });

  it("validates hostnames", () => {
    expect(validateHostname("docs.example.com")).toBeNull();
    expect(validateHostname("*.example.com")).toBeNull();
    expect(validateHostname("localhost")).not.toBeNull();
    expect(validateHostname("bad_name.example.com")).not.toBeNull();
    expect(validateHostname("a.".repeat(130) + "com")).not.toBeNull();
  });
});
