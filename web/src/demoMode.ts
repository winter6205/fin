export interface DemoStatus {
  enabled: boolean;
}

export interface DemoSourceLocator {
  document_id: string;
  version: string;
  unit: number;
  page: number;
}

export interface DemoSourceSummary extends DemoSourceLocator {
  title: string;
  section: string;
}

export interface DemoSource extends DemoSourceSummary {
  content: string;
}

export class DemoApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, status: number) {
    super(code);
    this.name = "DemoApiError";
    this.code = code;
    this.status = status;
  }
}

type DemoFetcher = typeof fetch;

const DEMO_ERROR_COPY: Record<string, string> = {
  demo_host_unavailable: "演示服务暂不可用。",
  demo_status_unavailable: "演示服务暂不可用。",
  demo_configuration_missing: "演示重置配置不完整，请重新启动 fin demo。",
  demo_configuration_invalid: "演示重置配置不完整，请重新启动 fin demo。",
  demo_mode_disabled: "当前服务未处于演示模式。",
  demo_reset_in_progress: "演示数据正在重置，请稍候。",
  demo_seed_refused: "演示环境安全检查未通过，数据未重置。",
  malformed_demo_reset: "演示数据重置失败，请检查 fin 服务状态。",
  demo_reset_failed: "演示数据重置失败，请检查 fin 服务状态。",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isDemoSourceSummary(value: unknown): value is DemoSourceSummary {
  return isRecord(value) && hasDemoSourceLocator(value) && hasDemoSourceLabels(value);
}

function isNonEmptyText(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isContentVersion(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value > 0;
}

function hasDemoSourceLocator(value: Record<string, unknown>): boolean {
  return (
    isNonEmptyText(value.document_id) &&
    isContentVersion(value.version) &&
    isPositiveInteger(value.unit) &&
    isPositiveInteger(value.page)
  );
}

function hasDemoSourceLabels(value: Record<string, unknown>): boolean {
  return isNonEmptyText(value.title) && isNonEmptyText(value.section);
}

function isExactDemoSource(
  value: unknown,
  locator: DemoSourceLocator,
): value is DemoSource {
  if (!isRecord(value) || !isDemoSourceSummary(value)) return false;
  return (
    hasMatchingLocator(value, locator) &&
    isNonEmptyText(value.content)
  );
}

function hasMatchingLocator(
  source: DemoSourceSummary,
  locator: DemoSourceLocator,
): boolean {
  return (
    source.document_id === locator.document_id &&
    source.version === locator.version &&
    source.unit === locator.unit &&
    source.page === locator.page
  );
}

function sourceApiErrorCode(response: Response): Promise<string> {
  return responseErrorCode(response, "demo_source_unavailable");
}

async function requestDemo(
  fetcher: DemoFetcher,
  url: string,
  init?: RequestInit,
): Promise<Response> {
  try {
    return init === undefined
      ? await fetcher(url)
      : await fetcher(url, init);
  } catch (error) {
    // EXIT: an unreachable host becomes a fixed, safe product error.
    if (error instanceof DemoApiError) throw error;
    throw new DemoApiError("demo_host_unavailable", 0);
  }
}

async function responseErrorCode(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const payload: unknown = await response.json();
    if (isRecord(payload) && typeof payload.error === "string") {
      return payload.error;
    }
  } catch {
    // EXIT: an invalid error body uses the stable operation-specific code.
    return fallback;
  }
  return fallback;
}

export async function getDemoStatus(
  fetcher: DemoFetcher = fetch,
): Promise<DemoStatus> {
  const response = await requestDemo(fetcher, "/api/demo");
  if (!response.ok) {
    throw new DemoApiError("demo_status_unavailable", response.status);
  }
  const payload: unknown = await response.json();
  if (!isRecord(payload) || typeof payload.enabled !== "boolean") {
    throw new DemoApiError("malformed_demo_status", response.status);
  }
  return { enabled: payload.enabled };
}

export async function getDemoSourceIndex(
  fetcher: DemoFetcher = fetch,
): Promise<DemoSourceSummary[]> {
  const response = await requestDemo(fetcher, "/api/demo/sources");
  if (!response.ok) {
    throw new DemoApiError(await sourceApiErrorCode(response), response.status);
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    // EXIT: malformed JSON cannot produce clickable demo citations.
    throw new DemoApiError("malformed_demo_sources", response.status);
  }
  if (
    !isRecord(payload) ||
    !Array.isArray(payload.sources) ||
    !payload.sources.every(isDemoSourceSummary)
  ) {
    throw new DemoApiError("malformed_demo_sources", response.status);
  }
  return payload.sources;
}

export async function getDemoSource(
  locator: DemoSourceLocator,
  fetcher: DemoFetcher = fetch,
): Promise<DemoSource> {
  const query = new URLSearchParams({
    document_id: locator.document_id,
    version: locator.version,
    unit: String(locator.unit),
    page: String(locator.page),
  });
  const response = await requestDemo(fetcher, `/api/demo/source?${query}`);
  if (!response.ok) {
    throw new DemoApiError(await sourceApiErrorCode(response), response.status);
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    // EXIT: a response that cannot be parsed must not be displayed as a source.
    throw new DemoApiError("malformed_demo_source", response.status);
  }
  if (!isExactDemoSource(payload, locator)) {
    throw new DemoApiError("malformed_demo_source", response.status);
  }
  return payload;
}

export function demoSourceUrl(source: DemoSourceLocator): string {
  const query = new URLSearchParams({
    document_id: source.document_id,
    version: source.version,
    unit: String(source.unit),
    page: String(source.page),
  });
  return `/demo/source?${query}`;
}

export async function resetDemoData(
  fetcher: DemoFetcher = fetch,
): Promise<void> {
  const response = await requestDemo(fetcher, "/api/demo/reset", {
    method: "POST",
  });
  if (!response.ok) {
    const code = await responseErrorCode(response, "demo_reset_failed");
    throw new DemoApiError(code, response.status);
  }
  const payload: unknown = await response.json();
  if (!isRecord(payload) || payload.status !== "reset") {
    throw new DemoApiError("malformed_demo_reset", response.status);
  }
}

export function describeDemoError(error: unknown): string {
  if (!(error instanceof DemoApiError)) return "演示服务暂不可用。";
  return DEMO_ERROR_COPY[error.code] ?? "演示操作失败，请检查 fin 服务状态。";
}
