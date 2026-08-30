export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
    public readonly details?: unknown,
  ) {
    super(message);
  }
}

const unauthenticatedListeners = new Set<() => void>();

export function onUnauthenticated(listener: () => void): () => void {
  unauthenticatedListeners.add(listener);
  return () => unauthenticatedListeners.delete(listener);
}

type RequestOptions = Omit<RequestInit, "body"> & { json?: unknown; body?: BodyInit | null };

/** Same-origin JSON client. The browser session cookie is the only credential;
 *  nothing is ever placed in a query string. A 401 anywhere flips the app into
 *  the login card. */
export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Accept", "application/json");
  let body: BodyInit | null | undefined = options.body;
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(options.json);
  }
  const response = await fetch(path, { ...options, body, headers, credentials: "same-origin" });
  if (response.status === 401) {
    unauthenticatedListeners.forEach((listener) => listener());
    throw new ApiError(401, "unauthenticated", "尚未登入");
  }
  const text = await response.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = text;
  }
  if (!response.ok) {
    const record = (data && typeof data === "object" ? data : {}) as Record<string, unknown>;
    const error = (record.error && typeof record.error === "object" ? record.error : {}) as Record<string, unknown>;
    const message = String(error.message ?? record.detail ?? record.message ?? response.statusText);
    throw new ApiError(response.status, String(error.code ?? record.code ?? "http_error"), message, error.details);
  }
  return data as T;
}

export function websocketUrl(path: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${path}`;
}
