import { render, screen } from "@testing-library/react";
import net from "node:net";
import { describe, expect, it } from "vitest";
import { App } from "../App";
import { ApiClient } from "../api/client";

function closedPort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.once("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address() as net.AddressInfo;
      srv.close(() => resolve(port));
    });
  });
}

describe("backend unavailable", () => {
  it("shows the unreachable banner against a closed port", async () => {
    window.location.hash = "#/";
    const port = await closedPort();
    render(<App client={new ApiClient(`http://127.0.0.1:${port}`)} />);
    const banner = await screen.findByRole("alert", undefined, { timeout: 30_000 });
    expect(banner).toHaveTextContent(/unreachable/i);
  });
});
