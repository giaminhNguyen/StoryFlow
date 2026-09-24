import { render, screen } from "@testing-library/react";
import type { DisplayState } from "../api/types";
import { STATE_LABELS, StateBadge, describeState } from "./StateBadge";

const STATES = Object.keys(STATE_LABELS) as DisplayState[];

describe("StateBadge", () => {
  test("all eight states have distinct labels and explanations", () => {
    expect(STATES).toHaveLength(8);
    expect(new Set(Object.values(STATE_LABELS)).size).toBe(8);
    expect(new Set(STATES.map(describeState)).size).toBe(8);
  });

  test.each(STATES)("%s renders text label, class and tooltip", (state) => {
    render(<StateBadge state={state} />);
    const el = screen.getByText(STATE_LABELS[state], { exact: false });
    expect(el).toHaveClass(`badge-${state}`);
    expect(el).toHaveAttribute("title", describeState(state));
  });
});
