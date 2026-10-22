import { createServer, request as httpRequest } from "node:http";
import { once } from "node:events";
import { pipeline } from "node:stream/promises";

const MAX_ANSWER_FRAME_DELAY_MS = 5_000;
const UPSTREAM_FAILURE_COPY = "scripted SSE proxy upstream unavailable";
const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

class ScriptedProxyStartupError extends Error {
  constructor(message, cause) {
    super(message);
    this.name = "ScriptedProxyStartupError";
    this.cause = cause;
  }
}

class ScriptedProxyShutdownError extends Error {
  constructor(cause) {
    super("scripted SSE proxy could not finish shutting down");
    this.name = "ScriptedProxyShutdownError";
    this.cause = cause;
  }
}

class InvalidScriptedProxyTargetError extends Error {
  constructor(cause) {
    super("scripted SSE proxy accepts only same-origin paths");
    this.name = "InvalidScriptedProxyTargetError";
    this.cause = cause;
  }
}

class ScriptedProxyUpstreamError extends Error {
  constructor(cause) {
    super(UPSTREAM_FAILURE_COPY);
    this.name = "ScriptedProxyUpstreamError";
    this.cause = cause;
  }
}

/**
 * Start a loopback-only proxy for the scripted browser lane.
 *
 * SSE bodies are buffered to upstream completion, then written frame by
 * frame without changing their bytes. Only the interval between successive
 * non-empty `answer` frames is paced, so the browser tests can exercise its
 * scroll-follow behavior on already-admitted wire output. This helper is
 * intentionally used by `run-scripted-e2e.mjs` only.
 */
export async function startScriptedSseProxy({
  upstreamBaseUrl,
  answerFrameDelayMs = 0,
}) {
  const options = validateOptions({ upstreamBaseUrl, answerFrameDelayMs });
  const upstreamRequests = new Set();
  let closing = false;

  const server = createServer((request, response) => {
    if (closing) {
      // EXIT: the owner has begun shutdown, so this connection is not admitted.
      response.writeHead(503, { "content-type": "text/plain; charset=utf-8" });
      response.end("scripted SSE proxy is closing");
      return;
    }
    void forwardRequest(request, response, options, upstreamRequests);
  });

  try {
    await new Promise((resolve, reject) => {
      server.once("error", reject);
      server.listen(0, "127.0.0.1", resolve);
    });
  } catch (error) {
    throw new ScriptedProxyStartupError(
      "scripted SSE proxy failed to bind a loopback port",
      error,
    );
  }
  const address = server.address();
  if (!address || typeof address === "string") {
    // EXIT: without a TCP address the proxy cannot accept browser traffic.
    server.close();
    throw new ScriptedProxyStartupError(
      "scripted SSE proxy failed to bind a TCP port",
    );
  }

  let closePromise;
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    close() {
      if (closePromise) return closePromise;
      closing = true;
      for (const request of upstreamRequests) request.destroy();
      closePromise = new Promise((resolve, reject) => {
        server.close((error) => {
          if (error) {
            reject(new ScriptedProxyShutdownError(error));
            return;
          }
          resolve();
        });
        server.closeAllConnections();
      });
      return closePromise;
    },
  };
}

function validateOptions({ upstreamBaseUrl, answerFrameDelayMs }) {
  if (typeof upstreamBaseUrl !== "string" || upstreamBaseUrl.trim() === "") {
    throw new TypeError("upstreamBaseUrl must be a non-empty loopback URL");
  }
  const upstream = parseLoopbackOrigin(upstreamBaseUrl);
  validateAnswerFrameDelay(answerFrameDelayMs);
  return { upstream, answerFrameDelayMs };
}

function parseLoopbackOrigin(upstreamBaseUrl) {
  let upstream;
  try {
    upstream = new URL(upstreamBaseUrl);
  } catch (error) {
    // EXIT: URL parsing failed before any upstream request was created.
    throw new TypeError("upstreamBaseUrl must be a valid loopback URL", {
      cause: error,
    });
  }
  if (
    upstream.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(upstream.hostname) ||
    upstream.username !== "" ||
    upstream.password !== "" ||
    upstream.pathname !== "/" ||
    upstream.search !== "" ||
    upstream.hash !== ""
  ) {
    throw new TypeError("upstreamBaseUrl must name a plain loopback HTTP origin");
  }
  return upstream;
}

function validateAnswerFrameDelay(answerFrameDelayMs) {
  if (
    !Number.isInteger(answerFrameDelayMs) ||
    answerFrameDelayMs < 0 ||
    answerFrameDelayMs > MAX_ANSWER_FRAME_DELAY_MS
  ) {
    throw new RangeError(
      `answerFrameDelayMs must be an integer from 0 to ${MAX_ANSWER_FRAME_DELAY_MS}`,
    );
  }
}

async function forwardRequest(request, response, options, upstreamRequests) {
  const { upstream, answerFrameDelayMs } = options;
  let target;
  try {
    target = resolveProxyTarget(request, upstream);
  } catch (error) {
    if (!(error instanceof InvalidScriptedProxyTargetError)) {
      handleUpstreamFailure(response, error);
      return;
    }
    // EXIT: invalid request targets receive a local 400 without reaching upstream.
    writeBadRequest(response);
    return;
  }

  let upstreamRequest;
  try {
    upstreamRequest = createUpstreamRequest(request, target, upstream);
  } catch (error) {
    handleUpstreamFailure(response, error);
    return;
  }
  upstreamRequests.add(upstreamRequest);

  const abortUpstream = () => upstreamRequest.destroy();
  request.once("aborted", abortUpstream);
  response.once("close", () => {
    // EXIT: a downstream close before end aborts the matching upstream request.
    if (!response.writableEnded) abortUpstream();
  });

  try {
    const upstreamResponsePromise = new Promise((resolve, reject) => {
      upstreamRequest.once("response", resolve);
      upstreamRequest.once("error", reject);
    });
    request.pipe(upstreamRequest);
    const upstreamResponse = await upstreamResponsePromise;
    await relayUpstreamResponse(
      response,
      upstreamResponse,
      answerFrameDelayMs,
    );
  } catch (error) {
    handleUpstreamFailure(response, error);
  } finally {
    upstreamRequests.delete(upstreamRequest);
    request.off("aborted", abortUpstream);
  }
}

function createUpstreamRequest(request, target, upstream) {
  const headers = {
    ...request.headers,
    host: upstream.host,
    "accept-encoding": "identity",
  };
  for (const name of HOP_BY_HOP_HEADERS) delete headers[name];
  try {
    return httpRequest(target, { method: request.method, headers });
  } catch (error) {
    throw new ScriptedProxyUpstreamError(error);
  }
}

function resolveProxyTarget(request, upstream) {
  let target;
  try {
    target = new URL(request.url ?? "/", upstream);
  } catch (error) {
    throw new InvalidScriptedProxyTargetError(error);
  }
  if (target.origin !== upstream.origin) {
    throw new InvalidScriptedProxyTargetError();
  }
  return target;
}

async function relayUpstreamResponse(response, upstreamResponse, answerFrameDelayMs) {
  const responseHeaders = copyResponseHeaders(upstreamResponse.headers);
  if (!isEventStream(upstreamResponse.headers["content-type"])) {
    response.writeHead(upstreamResponse.statusCode ?? 502, responseHeaders);
    await pipeline(upstreamResponse, response);
    return;
  }

  const body = await collect(upstreamResponse);
  if (response.destroyed) {
    // EXIT: the browser disconnected while the complete SSE response was buffered.
    return;
  }
  delete responseHeaders["content-length"];
  responseHeaders["content-length"] = String(body.length);
  response.writeHead(upstreamResponse.statusCode ?? 502, responseHeaders);
  await replaySseFrames(response, body, answerFrameDelayMs);
}

function isEventStream(contentTypeHeader) {
  return String(contentTypeHeader ?? "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase() === "text/event-stream";
}

function handleUpstreamFailure(response, cause) {
  const failure = new ScriptedProxyUpstreamError(cause);
  if (response.destroyed) {
    // EXIT: the browser disconnected, so there is no response channel left to report on.
    return;
  }
  if (response.headersSent) {
    // EXIT: a partial upstream response cannot be replaced with an HTTP error body.
    response.destroy();
    return;
  }
  response.writeHead(502, { "content-type": "text/plain; charset=utf-8" });
  response.end(failure.message);
}

function copyResponseHeaders(headers) {
  const result = {};
  for (const [name, value] of Object.entries(headers)) {
    if (value !== undefined && !HOP_BY_HOP_HEADERS.has(name)) {
      result[name] = value;
    }
  }
  return result;
}

async function collect(response) {
  const chunks = [];
  for await (const chunk of response) chunks.push(Buffer.from(chunk));
  return Buffer.concat(chunks);
}

async function replaySseFrames(response, body, answerFrameDelayMs) {
  let answerCount = 0;
  for (const frame of splitSseFrames(body)) {
    // EXIT: a closed downstream cannot receive any remaining buffered frames.
    if (response.destroyed) return;
    if (isNonemptyAnswerFrame(frame)) {
      if (answerCount > 0) await waitUnlessClosed(response, answerFrameDelayMs);
      answerCount += 1;
    }
    // EXIT: the browser may close during the answer-frame pacing interval.
    if (response.destroyed) return;
    if (!response.write(frame)) {
      await Promise.race([once(response, "drain"), once(response, "close")]);
    }
  }
  if (!response.destroyed) response.end();
}

function splitSseFrames(body) {
  const frames = [];
  let start = 0;
  while (start < body.length) {
    const separator = findFrameSeparator(body, start);
    if (!separator) {
      frames.push(body.subarray(start));
      break;
    }
    const end = separator.index + separator.length;
    frames.push(body.subarray(start, end));
    start = end;
  }
  return frames;
}

function findFrameSeparator(body, start) {
  const candidates = [
    [Buffer.from("\r\n\r\n"), 4],
    [Buffer.from("\n\n"), 2],
    [Buffer.from("\r\r"), 2],
  ];
  let earliest = null;
  for (const [needle, length] of candidates) {
    const index = body.indexOf(needle, start);
    if (index >= 0 && (earliest === null || index < earliest.index)) {
      earliest = { index, length };
    }
  }
  return earliest;
}

function isNonemptyAnswerFrame(frame) {
  const text = frame.toString("utf8").replace(/(?:\r\n|\n|\r){2}$/, "");
  const lines = text.split(/\r\n|\n|\r/);
  if (!lines.includes("event: answer")) return false;
  const data = lines
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  try {
    const payload = JSON.parse(data);
    return typeof payload.text === "string" && payload.text.length > 0;
  } catch {
    // EXIT: malformed JSON is not paced as answer prose; its bytes still pass through.
    return false;
  }
}

function waitUnlessClosed(response, delayMs) {
  // EXIT: no timer is needed when pacing is disabled or downstream is closed.
  if (delayMs === 0 || response.destroyed) return Promise.resolve();
  return new Promise((resolve) => {
    const finish = () => {
      clearTimeout(timer);
      response.off("close", finish);
      resolve();
    };
    const timer = setTimeout(finish, delayMs);
    response.once("close", finish);
  });
}

function writeBadRequest(response) {
  response.writeHead(400, { "content-type": "text/plain; charset=utf-8" });
  response.end("scripted SSE proxy accepts only same-origin paths");
}
