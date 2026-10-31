import { vi } from "vitest";

/** What the Worker sent to an origin, captured before the response goes back. */
export interface SentRequest {
  url: string;
  method: string;
  headers: Headers;
  redirect: string;
  cache: string | undefined;
  body: Uint8Array | null;
}

/**
 * Replace outbound fetch with a fake origin and record every request the Worker sends.
 * The body is read inside the mock: a request stream cannot be read once the response is out.
 */
export function mockUpstream(
  respond: (sent: SentRequest) => Response = () => new Response("upstream ok"),
): SentRequest[] {
  const sent: SentRequest[] = [];
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const request = new Request(input, init);
    const record: SentRequest = {
      url: request.url,
      method: request.method,
      headers: request.headers,
      redirect: request.redirect,
      cache: request.cache,
      body: request.body === null ? null : new Uint8Array(await request.arrayBuffer()),
    };
    sent.push(record);
    return respond(record);
  });
  return sent;
}
