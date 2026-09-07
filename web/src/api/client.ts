// Typed fetch wrapper. Same-origin, cookie-based session (credentials:'include'),
// so no token handling in JS. A 401 means the session is gone -> bounce to login.
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 30_000);
  const cancel = () => controller.abort();
  signal?.addEventListener("abort", cancel, { once: true });
  if (signal?.aborted) cancel();
  let res: Response;
  let text: string;
  try {
    res = await fetch(path, {
      signal: controller.signal,
      method,
      credentials: "include",
      headers:
        body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    text = await res.text();
  } catch (error) {
    if (signal?.aborted)
      throw new DOMException("Request cancelled", "AbortError");
    if (controller.signal.aborted)
      throw new ApiError(
        408,
        "Request timed out. Refresh the workload list before retrying.",
      );
    throw new ApiError(503, "Unable to reach the service. Please retry.");
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener("abort", cancel);
  }
  if (res.status === 401 && path !== "/auth/login" && path !== "/auth/me") {
    window.dispatchEvent(new CustomEvent("arise-unauthorized"));
  }
  let data: any = undefined;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      throw new ApiError(
        res.ok ? 502 : res.status,
        `HTTP ${res.status}: invalid service response`,
      );
    }
  }
  if (!res.ok)
    throw new ApiError(
      res.status,
      (data && data.error) || `HTTP ${res.status}`,
    );
  return data as T;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) =>
    request<T>("GET", path, undefined, signal),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};
