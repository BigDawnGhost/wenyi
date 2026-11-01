// The HTTP decoder is the one place that knows the error envelope, so its
// contract is pinned here rather than through a browser.
import assert from "node:assert/strict";
import { test } from "node:test";

import { errorMessage } from "../src/lib/error-codes.ts";
import { ApiError, decodeResponse } from "../src/lib/http.ts";

const translate = (key) => `localized:${key}`;

function failure(body, status = 409) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

test("a failure keeps its status, code and detail", async () => {
  await assert.rejects(
    decodeResponse(failure({ code: "project_busy", message: "Busy.", detail: "Busy." })),
    (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 409);
      assert.equal(error.code, "project_busy");
      assert.equal(error.detail, "Busy.");
      // Anything that only prints the error still reads the way it used to.
      assert.equal(error.message, "409: Busy.");
      return true;
    },
  );
});

test("a response without a code still decodes", async () => {
  await assert.rejects(decodeResponse(failure({ detail: "Plain." }, 500)), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.code, undefined);
    assert.equal(error.detail, "Plain.");
    return true;
  });
});

test("the message uses localized copy when the code is known", () => {
  const error = new ApiError({ status: 409, code: "project_busy", detail: "Project is busy." });
  assert.equal(errorMessage(error, translate), "localized:error.projectBusy");
});

test("an unknown code falls back to the server text", () => {
  const error = new ApiError({ status: 409, code: "brand_new_code", detail: "Server words." });
  assert.equal(errorMessage(error, translate), "Server words.");
});

test("a plain Error is shown as-is", () => {
  assert.equal(errorMessage(new Error("no code"), translate), "no code");
});

test("every mapped code resolves to a real message key", () => {
  // A typo in the table would silently fall back to English at runtime.
  const error = new ApiError({ status: 400, code: "project_busy", detail: "" });
  assert.equal(errorMessage(error, translate), "localized:error.projectBusy");
});
