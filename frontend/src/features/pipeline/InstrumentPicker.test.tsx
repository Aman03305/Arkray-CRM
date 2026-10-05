import { createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { INSTRUMENT_DRAG_TYPE, InstrumentPicker } from "./InstrumentPicker";

const INSTRUMENTS = ["Adams 8380 V-lite", "Adams 8180 V", "Adams 8180 T", "PCBA with Printer"];

function Picker({ initial = "", onChange = vi.fn(), errors }: { initial?: string; onChange?: (value: string) => void; errors?: string[] }) {
  const [value, setValue] = useState(initial);
  return (
    <InstrumentPicker
      instruments={INSTRUMENTS}
      value={value}
      errors={errors}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
}

const group = () => screen.getByRole("radiogroup", { name: "Instrument name (optional)" });
const option = (name: string) => within(group()).getByRole("radio", { name });
const zone = () => screen.getByTestId("instrument-drop-zone");

/** A drag's data, as a browser carries it between dragstart and drop. */
function transfer(data: Record<string, string> = {}) {
  const store = { ...data };
  return {
    get types() {
      return Object.keys(store);
    },
    setData: (type: string, value: string) => {
      store[type] = value;
    },
    getData: (type: string) => store[type] ?? "",
    dropEffect: "none",
    effectAllowed: "all",
  };
}

function drag(element: Element, type: "dragStart" | "dragOver" | "drop" | "dragEnd", dataTransfer: ReturnType<typeof transfer>) {
  const event = createEvent[type](element);
  Object.defineProperty(event, "dataTransfer", { value: dataTransfer });
  fireEvent(element, event);
  return event;
}

describe("the instrument picker", () => {
  it("offers exactly the four instruments, none chosen, as a labelled radio group", () => {
    render(<Picker />);
    expect(within(group()).getAllByRole("radio").map((r) => r.textContent)).toEqual(INSTRUMENTS);
    for (const name of INSTRUMENTS) expect(option(name)).toHaveAttribute("aria-checked", "false");
    expect(zone()).toHaveTextContent("Drag or click an instrument");
    // Only one tab stop: the first option until one is chosen.
    expect(within(group()).getAllByRole("radio").map((r) => r.tabIndex)).toEqual([0, -1, -1, -1]);
  });

  it("chooses one with a click, and shows it as the selected instrument", async () => {
    const onChange = vi.fn();
    render(<Picker onChange={onChange} />);
    await userEvent.setup().click(option("Adams 8180 T"));
    expect(onChange).toHaveBeenLastCalledWith("Adams 8180 T");
    expect(option("Adams 8180 T")).toHaveAttribute("aria-checked", "true");
    expect(option("Adams 8180 V")).toHaveAttribute("aria-checked", "false");
    expect(within(zone()).getByText("Adams 8180 T")).toBeInTheDocument();
  });

  it("chooses one with a tap on a touch screen (no dragging needed)", async () => {
    const user = userEvent.setup();
    render(<Picker />);
    await user.pointer({ keys: "[TouchA]", target: option("PCBA with Printer") });
    expect(option("PCBA with Printer")).toHaveAttribute("aria-checked", "true");
    expect(within(zone()).getByText("PCBA with Printer")).toBeInTheDocument();
  });

  it("works from the keyboard: Tab in, arrows move and choose, Space and Enter choose", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <>
        <button type="button">before</button>
        <Picker onChange={onChange} />
      </>,
    );
    await user.click(screen.getByRole("button", { name: "before" }));
    await user.tab();
    expect(option("Adams 8380 V-lite")).toHaveFocus();
    await user.keyboard(" ");
    expect(option("Adams 8380 V-lite")).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{ArrowDown}");
    expect(option("Adams 8180 V")).toHaveFocus();
    expect(option("Adams 8180 V")).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{ArrowUp}{ArrowUp}");
    expect(option("PCBA with Printer")).toHaveFocus(); // wraps around
    await user.keyboard("{Home}");
    expect(option("Adams 8380 V-lite")).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{End}");
    expect(option("PCBA with Printer")).toHaveAttribute("aria-checked", "true");
    await user.keyboard("{ArrowLeft}{Enter}");
    expect(onChange).toHaveBeenLastCalledWith("Adams 8180 T");
    // The chosen one is now the group's single tab stop.
    expect(within(group()).getAllByRole("radio").map((r) => r.tabIndex)).toEqual([-1, -1, 0, -1]);
  });

  it("chooses one dragged into the Selected box", () => {
    const onChange = vi.fn();
    render(<Picker onChange={onChange} />);
    const data = transfer();
    drag(option("Adams 8180 V"), "dragStart", data);
    expect(data.getData(INSTRUMENT_DRAG_TYPE)).toBe("Adams 8180 V");
    const over = drag(zone(), "dragOver", data);
    expect(over.defaultPrevented).toBe(true); // the box accepts the drop
    drag(zone(), "drop", data);
    expect(onChange).toHaveBeenLastCalledWith("Adams 8180 V");
    expect(option("Adams 8180 V")).toHaveAttribute("aria-checked", "true");
    expect(within(zone()).getByText("Adams 8180 V")).toBeInTheDocument();
  });

  it("ignores anything else dropped on the box (text from elsewhere, an unknown name)", () => {
    const onChange = vi.fn();
    render(<Picker onChange={onChange} />);
    const text = transfer({ "text/plain": "Adams 8180 V" });
    expect(drag(zone(), "dragOver", text).defaultPrevented).toBe(false);
    drag(zone(), "drop", text);
    drag(zone(), "drop", transfer({ [INSTRUMENT_DRAG_TYPE]: "Adams 9999 <b>" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(zone()).toHaveTextContent("Drag or click an instrument");
  });

  it("clears the choice with the Selected box's remove button", async () => {
    const onChange = vi.fn();
    render(<Picker initial="Adams 8380 V-lite" onChange={onChange} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Remove Adams 8380 V-lite" }));
    expect(onChange).toHaveBeenLastCalledWith("");
    expect(option("Adams 8380 V-lite")).toHaveAttribute("aria-checked", "false");
  });

  it("shows an older opportunity's own instrument text until another is chosen", () => {
    render(<Picker initial="HbA1c analyser" />);
    expect(zone()).toHaveTextContent("HbA1c analyser(not in the list)");
    for (const name of INSTRUMENTS) expect(option(name)).toHaveAttribute("aria-checked", "false");
  });

  it("states a problem on the group; focus sent to the group goes on to its tab stop", () => {
    render(<Picker initial="Adams 8180 T" errors={["Choose an instrument from the list."]} />);
    expect(group()).toHaveAttribute("aria-invalid", "true");
    expect(group()).toHaveAccessibleDescription("Choose an instrument from the list.");
    group().focus(); // what the form does with its first invalid field
    expect(option("Adams 8180 T")).toHaveFocus(); // the arrow keys work at once
  });

  it("keeps focus in the picker when the choice is removed with the keyboard", async () => {
    const user = userEvent.setup();
    render(<Picker initial="Adams 8180 V" />);
    screen.getByRole("button", { name: "Remove Adams 8180 V" }).focus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(option("Adams 8380 V-lite")).toHaveFocus());
    expect(document.activeElement).not.toBe(document.body);
  });

  it("never calls a value unlisted while the list loads or after it failed", () => {
    const { rerender } = render(<InstrumentPicker instruments={[]} value="Adams 8180 V" onChange={vi.fn()} status="loading" />);
    expect(screen.queryByText("(not in the list)")).not.toBeInTheDocument();
    rerender(<InstrumentPicker instruments={[]} value="Adams 8180 V" onChange={vi.fn()} status="error" />);
    expect(screen.queryByText("(not in the list)")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/Instruments couldn.t be loaded/);
  });

  it("says when the list is loading or couldn't be loaded, and offers a retry", async () => {
    const retry = vi.fn();
    const { rerender } = render(<InstrumentPicker instruments={[]} value="" onChange={vi.fn()} status="loading" />);
    expect(screen.getByText("Loading instruments")).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
    rerender(<InstrumentPicker instruments={[]} value="" onChange={vi.fn()} status="error" onRetry={retry} />);
    expect(screen.getByText(/Instruments couldn.t be loaded/)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(retry).toHaveBeenCalled();
  });
});
