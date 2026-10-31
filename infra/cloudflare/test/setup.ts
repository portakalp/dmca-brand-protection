import { afterEach, beforeEach, vi } from "vitest";

// No test may reach the network: outbound fetch fails unless a test mocks it.
beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockRejectedValue(new Error("unexpected outbound fetch"));
});

afterEach(() => {
  vi.restoreAllMocks();
});
