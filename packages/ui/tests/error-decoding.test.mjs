// The HTTP decoder is the one place that knows the error envelope, so its
// contract is pinned here rather than through a browser.
import assert from "node:assert/strict";
import { test } from "node:test";

import { errorMessage } from "../src/lib/error-codes.ts";
import {
  ApiError,
  REQUEST_TIMEOUT_MS,
  decodeResponse,
  sendRequest,
  timeoutForMethod,
  withTimeout,
} from "../src/lib/http.ts";

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

test("a socket failure frame decodes like an HTTP failure", () => {
  // The progress socket reports a failed run as plain JSON, never as a thrown
  // response, so the envelope decoder must not depend on an Error instance.
  const frame = { code: "project_busy", message: "The project is busy." };
  assert.equal(errorMessage(frame, translate), "localized:error.projectBusy");
});

test("an unknown socket code keeps the frame's own message", () => {
  const frame = { code: "pipeline_failed", message: "Provider connection failed" };
  assert.equal(errorMessage(frame, translate), "Provider connection failed");
});

test("a plain object without envelope fields still prints readably", () => {
  // Widening the envelope must not turn arbitrary values into "[object Object]".
  assert.equal(errorMessage({}, translate), "[object Object]");
  assert.equal(errorMessage("A bare string.", translate), "A bare string.");
});

test("a stalled request fails as request_timeout instead of staying pending", async () => {
  const stalled = () =>
    Promise.reject(new DOMException("The operation timed out.", "TimeoutError"));
  await assert.rejects(sendRequest(stalled), (error) => {
    assert.ok(error instanceof ApiError, "the deadline must produce the shared shape");
    assert.equal(error.status, 0);
    assert.equal(error.code, "request_timeout");
    assert.equal(errorMessage(error, translate), "localized:error.requestTimeout");
    return true;
  });
});

test("a caller's own cancellation is not reported as a failure", async () => {
  const canceled = () =>
    Promise.reject(new DOMException("The operation was aborted.", "AbortError"));
  await assert.rejects(sendRequest(canceled), (error) => {
    // React Query cancels superseded reads; those must stay cancellations.
    assert.ok(!(error instanceof ApiError));
    assert.equal(error.name, "AbortError");
    return true;
  });
});

test("reads, writes and downloads keep their own deadlines", () => {
  assert.equal(timeoutForMethod(undefined), REQUEST_TIMEOUT_MS.read);
  assert.equal(timeoutForMethod("GET"), REQUEST_TIMEOUT_MS.read);
  assert.equal(timeoutForMethod("head"), REQUEST_TIMEOUT_MS.read);
  assert.equal(timeoutForMethod("POST"), REQUEST_TIMEOUT_MS.write);
  assert.equal(timeoutForMethod("DELETE"), REQUEST_TIMEOUT_MS.write);
  assert.ok(REQUEST_TIMEOUT_MS.download > REQUEST_TIMEOUT_MS.write);
  assert.ok(REQUEST_TIMEOUT_MS.write > REQUEST_TIMEOUT_MS.read);
});

test("withTimeout bounds the request and keeps caller cancellation", () => {
  const bounded = withTimeout(undefined, 1000);
  assert.ok(bounded.signal instanceof AbortSignal);
  const controller = new AbortController();
  const composed = withTimeout({ method: "POST", signal: controller.signal }, 1000);
  // The caller's controller must still be able to abort the composed signal.
  controller.abort();
  assert.equal(composed.signal.aborted, true);
  assert.equal(composed.method, "POST");
});

test("an expired deadline aborts the request it bounds", async () => {
  const init = withTimeout(undefined, 5);
  assert.equal(init.signal.aborted, false);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(init.signal.aborted, true);
  assert.equal(init.signal.reason.name, "TimeoutError");
});
