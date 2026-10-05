// The studio's project field slugs names by the same rule as luna_director/store.py project_slug,
// so every /luna route accepts the project the user typed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { projectSlug } from "../../web/director/ui/dom.mjs";

test("names become [a-z0-9-] slugs like the server's", () => {
  assert.equal(projectSlug("My Shoot"), "my-shoot");
  assert.equal(projectSlug("  Café — Luna!! "), "cafe-luna");
  assert.equal(projectSlug("a".repeat(60)), "a".repeat(48));
  assert.equal(projectSlug("x".repeat(47) + " y"), "x".repeat(47));
});

test("empty becomes default; reserved Windows names get a suffix", () => {
  assert.equal(projectSlug(""), "default");
  assert.equal(projectSlug("!!!"), "default");
  assert.equal(projectSlug(null), "default");
  assert.equal(projectSlug("CON"), "con-project");
  assert.equal(projectSlug("lpt9"), "lpt9-project");
});
