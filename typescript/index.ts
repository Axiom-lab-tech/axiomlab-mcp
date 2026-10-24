/** Zero runtime dependencies. Request and response types come from the checked-in OpenAPI contract. */
import type { components } from "./schema.js";
export type { components, paths } from "./schema.js";
type S = components["schemas"];
export type Workflow = "auto" | "explainer" | "short_drama" | "creative_video" | "motion_explainer" | "product_pitch" | "profile_card";
export type VideoType = "project" | "product" | "personal" | "corporate" | "name_card";
export type VideoOptions = Partial<S["Options"]> & {
  motion_style?: "mono" | "photo"; photo_subject?: string; photo_license?: "strict" | "sharealike";  // motion_explainer only
};
export type VideoRequest = Omit<S["CreateVideo"], "options" | "prompt"> & { prompt?: string; options?: VideoOptions };  // a plan_id alone is a request
export type Video = S["Job"];
export type Plan = S["Plan"];
export type Page<T> = { data: T[]; next_cursor: string | number | null; has_more: boolean };
export type Query = Record<string, string | number | boolean | null | undefined>;
type Json = Record<string, unknown>;
const encode = encodeURIComponent;
const query = (path: string, params: Query = {}) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== null) search.set(key, String(value));
  return path + (search.size ? `?${search}` : "");
};

export class VidgenError extends Error {
  readonly code: string;
  readonly details: unknown;
  readonly requestId?: string;
  readonly retryAfter?: number;
  constructor(readonly status: number, readonly body: unknown, readonly headers = new Headers()) {
    const payload = body as {error?: {code?: string; message?: string; details?: unknown; request_id?: string}; detail?: string};
    const error = payload?.error;
    super(error?.message || (typeof payload?.detail === "string" ? payload.detail : `vidgen HTTP ${status}`));
    this.name = "VidgenError";
    this.code = error?.code || `http_${status}`;
    this.details = error?.details;
    this.requestId = error?.request_id || headers.get("X-Request-ID") || undefined;
    const retry = headers.get("Retry-After");
    if (retry) {
      const numeric = Number(retry);
      const seconds = Number.isFinite(numeric) ? numeric : (Date.parse(retry) - Date.now()) / 1000;
      if (Number.isFinite(seconds)) this.retryAfter = Math.max(0, seconds);
    }
  }
}

const contactSales = () => new VidgenError(403, {error: {code: "contact_sales", message: "Pricing is agreed per customer through sales; contact sales@axiomlab.online.", details: null, request_id: null}});

export class Vidgen {
  readonly baseUrl: string;
  constructor(baseUrl: string, readonly apiKey: string, readonly projectId?: string,
              readonly transport: typeof fetch = fetch,
              readonly options: {maxRetries?: number; timeoutMs?: number; sleep?: (ms: number) => Promise<void>} = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    if ((options.maxRetries ?? 3) < 0 || (options.timeoutMs ?? 90_000) <= 0) throw new Error("invalid retry/timeout options");
  }
  private sleep(ms: number) { return this.options.sleep ? this.options.sleep(ms) : new Promise<void>(resolve => setTimeout(resolve, ms)); }
  async request<T = unknown>(method: string, path: string, body?: unknown, idempotencyKey?: string, raw = false): Promise<T> {
    const headers: Record<string, string> = {Authorization: `Bearer ${this.apiKey}`};
    if (this.projectId) headers["X-Project-ID"] = this.projectId;
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    const multipart = body instanceof FormData;
    if (body !== undefined && !multipart) headers["Content-Type"] = "application/json";
    const safe = ["GET", "HEAD", "DELETE", "PUT"].includes(method.toUpperCase()) || !!idempotencyKey;
    for (let attempt = 0; ; attempt++) {
      let response: Response;
      try {
        response = await this.transport(this.baseUrl + path, {method, headers,
          body: body === undefined ? undefined : multipart ? body : JSON.stringify(body),
          signal: AbortSignal.timeout(this.options.timeoutMs ?? 90_000)});
      } catch (error) {
        if (!safe || attempt >= (this.options.maxRetries ?? 3)) throw error;
        await this.sleep(Math.min(30_000, 500 * 2 ** attempt));
        continue;
      }
      if (response.ok && raw) return await response.arrayBuffer() as T;
      const text = await response.text();
      let payload: unknown = null;
      try { payload = text ? JSON.parse(text) : null; } catch { payload = text.slice(0, 300); }
      if (response.ok) return payload as T;
      const error = new VidgenError(response.status, payload, response.headers);
      if (!safe || ![429, 500, 502, 503, 504].includes(response.status) || attempt >= (this.options.maxRetries ?? 3)) throw error;
      await this.sleep(error.retryAfter === undefined ? Math.min(30_000, 500 * 2 ** attempt) : error.retryAfter * 1000);
    }
  }
  private post<T>(path: string, body?: unknown, key: string = crypto.randomUUID()) { return this.request<T>("POST", path, body, key); }
  async *paginate<T>(path: string, params: Query = {}): AsyncGenerator<T> {
    let after = params.after;
    for (;;) {
      const page = await this.request<Page<T>>("GET", query(path, {...params, after}));
      yield* page.data;
      if (!page.has_more || !page.data.length || page.next_cursor === null || page.next_cursor === after) return;
      after = page.next_cursor;
    }
  }
  me() { return this.request<S["Me"]>("GET", "/v1/me"); }
  capabilities() { return this.request<S["Capabilities"]>("GET", "/v1/workflows"); }
  videoTypes() { return this.request<Page<S["VideoType"]>>("GET", "/v1/video-types"); }
  skills() { return this.request<S["Skills"]>("GET", "/v1/workflows/creative_video/skills"); }
  /** @deprecated Removed: prices are agreed per customer through sales and the API never quotes them. Always throws the `contact_sales` error (HTTP 403). */
  quote(_options?: VideoOptions): never { throw contactSales(); }
  route(body: VideoRequest, key?: string) { return this.post<S["RoutePreview"]>("/v1/route", body, key); }
  /** @deprecated Removed: there is no published price list. Always throws the `contact_sales` error (HTTP 403). */
  prices(): never { throw contactSales(); }
  wallet() { return this.request<S["OrganizationWallet"]>("GET", "/v1/wallet"); }
  walletEntries(params: Query = {}) { return this.paginate<S["WalletEntry"]>("/v1/wallet/entries", params); }
  checkout(amountCents: number, key?: string) { return this.post<S["Checkout"]>("/v1/wallet/checkout", {amount_cents: amountCents}, key); }
  usage(params: Query = {}) { return this.request<S["Usage"]>("GET", query("/v1/usage", params)); }
  projects(params: Query = {}) { return this.paginate<S["Project-Output"]>("/v1/projects", params); }
  createProject(name: string, key?: string) { return this.post<S["Project-Output"]>("/v1/projects", {name}, key); }
  deleteProject(id: string) { return this.request<S["Deleted"]>("DELETE", `/v1/projects/${encode(id)}`); }
  apiKeys(params: Query = {}) { return this.paginate<S["ApiKey"]>("/v1/api-keys", params); }
  createApiKey(body: S["CreateKey"], key?: string) { return this.post<S["CreatedKey"]>("/v1/api-keys", body, key); }
  rotateApiKey(id: string, graceSeconds = 0, key?: string) { return this.post<S["RotatedKey"]>(query(`/v1/api-keys/${encode(id)}/rotate`, {grace_seconds: graceSeconds}), undefined, key); }
  revokeApiKey(id: string) { return this.request<S["RevokedKey"]>("DELETE", `/v1/api-keys/${encode(id)}`); }
  audit(params: Query = {}) { return this.paginate<S["Event"]>("/v1/audit", params); }
  upload(file: Blob, filename: string, key?: string) {
    const form = new FormData(); form.set("file", file, filename);
    return this.post<S["Asset"]>("/v1/assets", form, key);
  }
  assets(params: Query = {}) { return this.paginate<S["Asset"]>("/v1/assets", params); }
  getAsset(id: string) { return this.request<S["Asset"]>("GET", `/v1/assets/${encode(id)}`); }
  downloadAsset(id: string) { return this.request<ArrayBuffer>("GET", `/v1/assets/${encode(id)}/file`, undefined, undefined, true); }
  deleteAsset(id: string) { return this.request<S["Deleted"]>("DELETE", `/v1/assets/${encode(id)}`); }
  brands(params: Query = {}) { return this.paginate<S["Resource"]>("/v1/brands", params); }
  createBrand(body: S["BrandKit"], key?: string) { return this.post<S["Resource"]>("/v1/brands", body, key); }
  getBrand(id: string, version?: number) { return this.request<S["Resource"]>("GET", query(`/v1/brands/${encode(id)}`, {version})); }
  updateBrand(id: string, body: S["BrandKit"], expectedVersion: number) { return this.request<S["Resource"]>("PUT", query(`/v1/brands/${encode(id)}`, {expected_version: expectedVersion}), body); }
  deleteBrand(id: string) { return this.request<S["Deleted"]>("DELETE", `/v1/brands/${encode(id)}`); }
  templates(params: Query = {}) { return this.paginate<S["Resource"]>("/v1/templates", params); }
  createTemplate(body: S["Template"], key?: string) { return this.post<S["Resource"]>("/v1/templates", body, key); }
  getTemplate(id: string, version?: number) { return this.request<S["Resource"]>("GET", query(`/v1/templates/${encode(id)}`, {version})); }
  updateTemplate(id: string, body: S["Template"], expectedVersion: number) { return this.request<S["Resource"]>("PUT", query(`/v1/templates/${encode(id)}`, {expected_version: expectedVersion}), body); }
  deleteTemplate(id: string) { return this.request<S["Deleted"]>("DELETE", `/v1/templates/${encode(id)}`); }
  plan(body: VideoRequest, key?: string) { return this.post<Plan>("/v1/plans", body, key); }
  getPlan(id: string) { return this.request<Plan>("GET", `/v1/plans/${encode(id)}`); }
  plans(params: Query = {}) { return this.paginate<Plan>("/v1/plans", params); }
  create(body: VideoRequest, key?: string) { return this.post<S["CreatedJob"]>("/v1/videos", body, key); }
  get(id: string) { return this.request<Video>("GET", `/v1/videos/${encode(id)}`); }
  videos(params: Query = {}) { return this.paginate<Video>("/v1/videos", params); }
  cancel(id: string, key?: string) { return this.post<S["CancelOutcome"]>(`/v1/videos/${encode(id)}/cancel`, undefined, key); }
  delete(id: string) { return this.request<S["Deleted"]>("DELETE", `/v1/videos/${encode(id)}`); }
  resume(id: string, answer: Partial<S["ResumeVideo"]>, key?: string) { return this.post<Video>(`/v1/videos/${encode(id)}/resume`, answer, key); }
  /** Approve a `needs_input` job's approvable `budget_approval` stop (operator-billed organizations); the server computes the new limit. Prepaid, flat-priced jobs never ask (`approvable: false`, answered 409). */
  approveBudget(id: string, key?: string) { return this.resume(id, {approve_budget: true}, key); }
  /** Answer a `needs_input` job's `confirm_duration` stop with the film length in seconds, within its `min_duration_sec`–`max_duration_sec` (30–180). */
  confirmDuration(id: string, seconds: number, key?: string) { return this.resume(id, {duration_sec: seconds}, key); }
  revise(id: string, body: S["Revision"], key?: string) { return this.post<S["CreatedJob"]>(`/v1/videos/${encode(id)}/revisions`, body, key); }
  batch(items: VideoRequest[], name = "Batch", key?: string) { return this.post<S["Batch"]>("/v1/batches", {name, items}, key); }
  getBatch(id: string) { return this.request<S["Batch"]>("GET", `/v1/batches/${encode(id)}`); }
  batches(params: Query = {}) { return this.paginate<S["BatchSummary"]>("/v1/batches", params); }
  events(after = 0, limit = 50) { return this.request<Page<S["Event"]>>("GET", query("/v1/events", {after, limit})); }
  allEvents(after = 0) { return this.paginate<S["Event"]>("/v1/events", {after}); }
  webhooks(params: Query = {}) { return this.paginate<S["Webhook-Output"]>("/v1/webhooks", params); }
  createWebhook(body: Json, key?: string) { return this.post<S["WebhookWithSecret"]>("/v1/webhooks", body, key); }
  updateWebhook(id: string, body: Json, expectedVersion: number) { return this.request<S["Webhook-Output"]>("PUT", query(`/v1/webhooks/${encode(id)}`, {expected_version: expectedVersion}), body); }
  rotateWebhookSecret(id: string, expectedVersion: number, key?: string) { return this.post<S["WebhookWithSecret"]>(query(`/v1/webhooks/${encode(id)}/rotate-secret`, {expected_version: expectedVersion}), undefined, key); }
  deleteWebhook(id: string) { return this.request<S["Deleted"]>("DELETE", `/v1/webhooks/${encode(id)}`); }
  deliveries(params: Query = {}) { return this.paginate<S["Delivery"]>("/v1/webhook-deliveries", params); }
  replayDelivery(id: string, webhookId?: string, key?: string) { return this.post<S["ReplayOutcome"]>(query(`/v1/webhook-deliveries/${encode(id)}/replay`, {webhook_id: webhookId}), undefined, key); }
  async wait(id: string, timeout = 7_200_000, interval = 10_000): Promise<Video> {
    if (timeout <= 0 || interval <= 0) throw new Error("timeout and interval must be positive");
    const deadline = Date.now() + timeout;
    for (;;) {
      const job = await this.get(id);
      if (!["queued", "running"].includes(job.status)) return job;
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw new Error(`Job ${id} is still running; continue polling the same id`);
      await this.sleep(Math.min(interval, remaining));
    }
  }
  static async verifyWebhook(secret: string, header: string, body: Uint8Array, tolerance = 300): Promise<boolean> {
    const fields: Record<string, string> = Object.fromEntries(header.split(",").map(part => part.split("=", 2)));
    const at = Number(fields.t);
    if (!Number.isFinite(at) || Math.abs(Date.now() / 1000 - at) > tolerance || !/^[a-f0-9]{64}$/.test(fields.v1 || "")) return false;
    const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), {name: "HMAC", hash: "SHA-256"}, false, ["verify"]);
    const prefix = new TextEncoder().encode(`${at}.`), message = new Uint8Array(prefix.length + body.length);
    message.set(prefix); message.set(body, prefix.length);
    const signature = Uint8Array.from(fields.v1.match(/../g)!, value => parseInt(value, 16));
    return crypto.subtle.verify("HMAC", key, signature, message);
  }
}
