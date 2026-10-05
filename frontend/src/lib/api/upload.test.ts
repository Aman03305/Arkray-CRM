import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiUpload, CSRF_ENDPOINT } from "./client";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const PATH = "/api/v1/workspaces/me/activities/n1/attachments";

describe("apiUpload", () => {
  const fetchMock = vi.fn<typeof fetch>();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    document.cookie = "arkray_csrftoken=csrf-123";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
    document.cookie = "arkray_csrftoken=; expires=Thu, 01 Jan 1970 00:00:00 GMT";
  });

  it("posts the raw file with its percent-encoded name and the CSRF token", async () => {
    fetchMock.mockResolvedValue(jsonResponse(201, { id: "f1" }));
    const file = new File(["%PDF"], "ignored.pdf");
    await expect(apiUpload(PATH, file, "Quote (v2) ₹.pdf")).resolves.toEqual({ id: "f1" });
    const [path, init] = fetchMock.mock.calls[0]!;
    expect(path).toBe(PATH);
    expect(init?.method).toBe("POST");
    expect(init?.body).toBe(file);
    expect(init?.credentials).toBe("same-origin");
    const headers = init?.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/octet-stream");
    expect(headers["X-Filename"]).toBe(encodeURIComponent("Quote (v2) ₹.pdf"));
    expect(headers["X-CSRFToken"]).toBe("csrf-123");
    expect(headers.Accept).toBe("application/json");
  });

  it("refuses anything but a same-origin API path", async () => {
    await expect(apiUpload("https://evil.example/api/x", new Blob(["x"]), "x.txt")).rejects.toThrow(/same-origin/);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("retries once with a fresh token after csrf_failed, sending the file again", async () => {
    const file = new Blob(["hello"]);
    fetchMock
      .mockResolvedValueOnce(jsonResponse(403, { error: { code: "csrf_failed", message: "CSRF.", details: null, request_id: "r" } }))
      .mockImplementationOnce(async () => {
        document.cookie = "arkray_csrftoken=fresh-456";
        return new Response(null, { status: 204 });
      })
      .mockResolvedValueOnce(jsonResponse(201, { id: "f2" }));
    await expect(apiUpload(PATH, file, "a.txt")).resolves.toEqual({ id: "f2" });
    expect(fetchMock.mock.calls.map(([p]) => p)).toEqual([PATH, CSRF_ENDPOINT, PATH]);
    const retry = fetchMock.mock.calls[2]![1]!;
    expect(retry.body).toBe(file);
    expect((retry.headers as Record<string, string>)["X-CSRFToken"]).toBe("fresh-456");
  });

  it("maps the error envelope (413 file_too_large) to ApiError", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse(413, { error: { code: "file_too_large", message: "Files can be at most 10 MB.", details: null, request_id: "r1" } }),
    );
    const error = await apiUpload(PATH, new Blob(["x"]), "big.pdf").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 413, code: "file_too_large", message: "Files can be at most 10 MB.", requestId: "r1" });
  });

  it("allows two minutes by default, and still times out", async () => {
    const timeout = vi.spyOn(AbortSignal, "timeout");
    fetchMock.mockResolvedValueOnce(jsonResponse(201, { id: "f1" }));
    await apiUpload(PATH, new Blob(["x"]), "a.txt");
    expect(timeout).toHaveBeenCalledWith(120_000);
    fetchMock.mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    await expect(apiUpload(PATH, new Blob(["x"]), "a.txt", { timeoutMs: 20 })).rejects.toMatchObject({ status: 0, code: "timeout" });
  });
});
