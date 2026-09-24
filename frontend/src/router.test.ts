import { parseRoute, projectHref, workflowHref } from "./router";

describe("parseRoute", () => {
  test.each([
    ["", { name: "list" }],
    ["#", { name: "list" }],
    ["#/", { name: "list" }],
    ["#/workflows/abc", { name: "workflow", id: "abc" }],
    ["#/workflows/abc/", { name: "workflow", id: "abc" }],
    ["#/projects/p%201", { name: "project", id: "p 1" }],
    ["#/workflows/abc?x=1", { name: "workflow", id: "abc" }],
  ])("%s", (hash, expected) => {
    expect(parseRoute(hash)).toEqual(expected);
  });

  test.each(["#/nope", "#/workflows", "#/workflows/a/b", "#/workflows/%E0%A4%A"])("unknown %s", (hash) => {
    expect(parseRoute(hash).name).toBe("notfound");
  });

  test("href builders encode ids", () => {
    expect(workflowHref("a b")).toBe("#/workflows/a%20b");
    expect(projectHref("p")).toBe("#/projects/p");
  });
});
