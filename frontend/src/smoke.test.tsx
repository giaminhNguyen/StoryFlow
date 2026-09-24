import { render, screen } from "@testing-library/react";

test("test tooling works", () => {
  render(<p>hello</p>);
  expect(screen.getByText("hello")).toBeInTheDocument();
});
