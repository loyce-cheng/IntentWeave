export type Capability = Readonly<{ id: string; token: string }>;

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
  capability?: Capability,
): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set("Content-Type", "application/json");
  if (capability?.token) headers.set("X-Session-Token", capability.token);
  const response = await fetch("/api" + path, { ...options, headers });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      data && typeof data.detail === "string"
        ? data.detail
        : `输入未通过校验 (${response.status})`,
    );
  }
  return response.json();
}

type Stamp = { sessionId: string; generation: number; sequence: number };

/** A control may change running/model state without increasing engine revision. */
export class ResponseGuard {
  private generation = 0;
  private sequence = 0;
  private applied = 0;

  invalidate() {
    this.generation += 1;
  }

  capture(sessionId: string): Stamp {
    return {
      sessionId,
      generation: this.generation,
      sequence: ++this.sequence,
    };
  }

  current(stamp: Stamp, sessionId: string): boolean {
    return (
      stamp.sessionId === sessionId && stamp.generation === this.generation
    );
  }

  accept(
    stamp: Stamp,
    sessionId: string,
    revision: number,
    currentRevision: number,
  ) {
    if (
      !this.current(stamp, sessionId) ||
      revision < currentRevision ||
      (revision === currentRevision && stamp.sequence < this.applied)
    )
      return false;
    this.applied = stamp.sequence;
    return true;
  }
}
