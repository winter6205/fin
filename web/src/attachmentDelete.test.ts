// The single-attachment delete door: DELETE
// /api/threads/{thread_id}/attachments/{attachment_id}.
//
// The host's promise is that one attachment's access is voided BEFORE its
// bytes are touched, so every refusal it answers is about THAT attachment's
// identity or about a cleanup still owed — never about the upload that
// minted it.
//
// The store/api seam below owns the wire contract. The chip owns the control:
// the web suite has no DOM environment, so the row tree the composer ships is
// walked from its exported seam, the ``×`` is found by the id it names, and its
// real ``onClick`` is invoked against the real store action — which makes the
// binding itself (not the file text) the thing under test.

import { isValidElement, type ReactElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AttachFailedError, deleteThreadAttachment } from "./api";
import {
  describeAttachFailure,
  type AttachFailureNotice,
} from "./attachFailure";
import {
  AttachmentChipList,
  AttachmentDeleteNotice,
  type AttachmentChipDoor,
} from "./components/Composer";
import { useWorkspaceStore } from "./store";
import type { ThreadAttachment, ThreadDetail } from "./types";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";
const OTHER_THREAD_ID = "55555555-5555-4555-8555-555555555555";

/** The row control's own name: the attachment id the delete route answers. */
const DELETE_CONTROL_PREFIX = "attachment-delete-";
/** The chip's refusal slot, which is not the upload door's slot. */
const DELETE_NOTICE_TESTID = "attachment-delete-failure";

function attachment(
  id: string,
  overrides: Partial<ThreadAttachment> = {},
): ThreadAttachment {
  return {
    attachment_id: id,
    filename: `${id}.md`,
    size_bytes: 6,
    scope: "thread_attach",
    status: "ready",
    version: 1,
    unit_count: 2,
    searchable: true,
    failure_reason: null,
    ...overrides,
  };
}

function seedActiveThread(ids: string[]): void {
  const thread: ThreadDetail = {
    id: THREAD_ID,
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns: [],
  };
  useWorkspaceStore.setState({
    activeThread: thread,
    attachments: ids.map((id) => attachment(id)),
    pendingUploads: {},
  });
}

function deleteUrl(attachmentId: string, threadId = THREAD_ID): string {
  return (
    `/api/threads/${encodeURIComponent(threadId)}/attachments/` +
    encodeURIComponent(attachmentId)
  );
}

interface RecordedCall {
  url: string;
  method: string;
  body: unknown;
}

function stubFetch(
  handler: (url: string, init?: RequestInit) => Response,
): { calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : String(input);
      calls.push({ url, method: init?.method ?? "GET", body: init?.body });
      return handler(url, init);
    }),
  );
  return { calls };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("the delete door's wire contract", () => {
  it("addresses the shipped path with DELETE and no body", async () => {
    const { calls } = stubFetch((url) =>
      url === deleteUrl("att-1")
        ? jsonResponse({ ok: true, attachment_id: "att-1" })
        : jsonResponse({ error: "unknown_locator" }, 400),
    );

    await expect(deleteThreadAttachment(THREAD_ID, "att-1")).resolves.toEqual({
      ok: true,
      attachment_id: "att-1",
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].url).toBe(deleteUrl("att-1"));
    // A DELETE carries no input but its two path identities.
    expect(calls[0].body).toBeUndefined();
  });

  it("percent-encodes an identity that needs it", async () => {
    const { calls } = stubFetch(() =>
      jsonResponse({ ok: true, attachment_id: "a/b" }),
    );

    await deleteThreadAttachment(THREAD_ID, "a/b");

    expect(calls[0].url).toBe(deleteUrl("a/b"));
    expect(calls[0].url).not.toContain("a/b");
  });

  it.each([
    ["unknown_locator", 400],
    ["invalid_locator", 400],
    ["thread_not_found", 404],
    ["attachment_cleanup_failed", 500],
    ["attachment_store_failed", 500],
  ])("rejects with the named %s code at %i", async (code, status) => {
    stubFetch(() => jsonResponse({ error: code }, status));

    const error = await deleteThreadAttachment(THREAD_ID, "att-1").catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(AttachFailedError);
    expect((error as AttachFailedError).code).toBe(code);
    expect((error as AttachFailedError).status).toBe(status);
  });

  it("names an unparseable body by status rather than answering success", async () => {
    stubFetch(() => new Response("gateway opened then died", { status: 502 }));

    const error = await deleteThreadAttachment(THREAD_ID, "att-1").catch(
      (caught: unknown) => caught,
    );

    expect((error as AttachFailedError).code).toBe("http_502");
  });
});

describe("store.deleteAttachment", () => {
  it("drops only the confirmed record from the slice", async () => {
    seedActiveThread(["att-1", "att-2"]);
    stubFetch((url) =>
      url === deleteUrl("att-1")
        ? jsonResponse({ ok: true, attachment_id: "att-1" })
        : jsonResponse({ error: "unknown_locator" }, 400),
    );

    await useWorkspaceStore.getState().deleteAttachment(THREAD_ID, "att-1");

    expect(
      useWorkspaceStore.getState().attachments.map((a) => a.attachment_id),
    ).toEqual(["att-2"]);
  });

  it("keeps the chip when the host refuses, so the row cannot lie", async () => {
    // The removal is response-driven: an ``attachment_cleanup_failed`` answer
    // means the bytes are still owed and the record is still voided-only —
    // the operator must still see the file and be able to retry the delete.
    seedActiveThread(["att-1"]);
    stubFetch(() =>
      jsonResponse({ error: "attachment_cleanup_failed" }, 500),
    );

    const error = await useWorkspaceStore
      .getState()
      .deleteAttachment(THREAD_ID, "att-1")
      .then(() => null)
      .catch((caught: unknown) => caught);

    expect((error as AttachFailedError).code).toBe("attachment_cleanup_failed");
    expect(
      useWorkspaceStore.getState().attachments.map((a) => a.attachment_id),
    ).toEqual(["att-1"]);
  });

  it("clears the record when the retry lands", async () => {
    seedActiveThread(["att-1"]);
    let attempts = 0;
    stubFetch(() => {
      attempts += 1;
      return attempts === 1
        ? jsonResponse({ error: "attachment_cleanup_failed" }, 500)
        : jsonResponse({ ok: true, attachment_id: "att-1" });
    });

    const first = await useWorkspaceStore
      .getState()
      .deleteAttachment(THREAD_ID, "att-1")
      .catch((caught: unknown) => caught);
    expect(first).not.toBeNull();
    await useWorkspaceStore.getState().deleteAttachment(THREAD_ID, "att-1");

    expect(useWorkspaceStore.getState().attachments).toEqual([]);
    expect(attempts).toBe(2);
  });

  it("never removes another thread's slice", async () => {
    seedActiveThread(["att-1"]);
    stubFetch(() => jsonResponse({ ok: true, attachment_id: "att-1" }));

    await useWorkspaceStore
      .getState()
      .deleteAttachment(OTHER_THREAD_ID, "att-1");

    expect(
      useWorkspaceStore.getState().attachments.map((a) => a.attachment_id),
    ).toEqual(["att-1"]);
  });

  it("touches no upload gate state", async () => {
    seedActiveThread(["att-1"]);
    useWorkspaceStore.setState({ pendingUploads: { [THREAD_ID]: 1 } });
    stubFetch(() => jsonResponse({ ok: true, attachment_id: "att-1" }));

    await useWorkspaceStore.getState().deleteAttachment(THREAD_ID, "att-1");

    // A delete is not an upload: the send gate's count is the upload door's
    // own state and must survive this call untouched.
    expect(useWorkspaceStore.getState().pendingUploads).toEqual({
      [THREAD_ID]: 1,
    });
  });
});

describe("delete refusal copy", () => {
  it("names each delete state with its own sentence", () => {
    const kinds = [
      "unknown_locator",
      "invalid_locator",
      "attachment_cleanup_failed",
      "attachment_store_failed",
    ];
    const notices = kinds.map((kind) =>
      describeAttachFailure(new AttachFailedError(kind, kind === "unknown_locator" ? 400 : 500)),
    );

    for (const [index, notice] of notices.entries()) {
      expect(notice.kind).toBe(kinds[index]);
      expect(notice.message.trim()).not.toBe("");
    }
    // Distinct: an operator who is told "already gone" must not be able to
    // read it as "the store refused" and vice versa.
    const messages = new Set(notices.map((notice) => notice.message));
    expect(messages.size).toBe(kinds.length);
  });

  it("does not reuse the upload-door sentences", () => {
    const cleanup = describeAttachFailure(
      new AttachFailedError("attachment_cleanup_failed", 500),
    );
    const unknown = describeAttachFailure(
      new AttachFailedError("unknown_locator", 400),
    );
    // ``attachment_not_found`` is the send / read / retry doors' miss; ``unknown_locator``
    // is the delete door's. Both must say something the operator acts on
    // differently: keep citing vs. nothing left to do.
    expect(unknown.message).not.toBe(
      describeAttachFailure(new AttachFailedError("attachment_not_found", 400))
        .message,
    );
    expect(cleanup.message).toContain("重试");
  });
});

describe("the chip's delete control", () => {
  /** Every element the shipped row tree renders, function components
   * included, so a test can reach the real control the real JSX builds. */
  function nodesOf(node: ReactNode): ReactElement[] {
    if (Array.isArray(node)) return node.flatMap(nodesOf);
    if (!isValidElement(node)) return [];
    const element = node as ReactElement<{ children?: ReactNode }>;
    if (typeof element.type === "function") {
      const render = element.type as (props: unknown) => ReactNode;
      return [element, ...nodesOf(render(element.props))];
    }
    return [element, ...nodesOf(element.props.children)];
  }

  function propsOf<T extends object>(node: ReactElement): T {
    return node.props as unknown as T;
  }

  /** The visible line a rendered node reads as, components expanded. */
  function textOf(node: ReactNode): string {
    if (Array.isArray(node)) return node.map(textOf).join("");
    if (typeof node === "string" || typeof node === "number") {
      return String(node);
    }
    if (isValidElement(node)) {
      const element = node as ReactElement<{ children?: ReactNode }>;
      if (typeof element.type === "function") {
        const render = element.type as (props: unknown) => ReactNode;
        return textOf(render(element.props));
      }
      return textOf(element.props.children);
    }
    return "";
  }

  interface ChipControlProps {
    "data-testid"?: string;
    "aria-label"?: string;
    onClick?: () => void;
  }

  /** The ``×`` the composer renders for one row, found by the id it names. */
  function deleteControl(
    rows: ThreadAttachment[],
    door: AttachmentChipDoor,
    attachmentId: string,
  ): ReactElement<ChipControlProps> {
    const controls = nodesOf(
      AttachmentChipList({ attachments: rows, door }),
    ).filter(
      (node) =>
        propsOf<ChipControlProps>(node)["data-testid"] ===
        `${DELETE_CONTROL_PREFIX}${attachmentId}`,
    );
    expect(controls).toHaveLength(1);
    return controls[0];
  }

  /** Invokes the control's real ``onClick``, then drains the promise chain it
   * started: the shipped handler is fire-and-forget, and a macrotask tick runs
   * after every pending microtask. */
  async function click(control: ReactElement<ChipControlProps>): Promise<void> {
    expect(
      control.props.onClick,
      "the row's × must be wired to a delete handler",
    ).toBeTypeOf("function");
    control.props.onClick?.();
    await new Promise((resolve) => setTimeout(resolve, 0));
  }

  /** The store's own action, recorded on its way through: the row's id can
   * only reach the wire if the control names the store's delete path. */
  function chipDoor(overrides: {
    onDeleted: () => void;
    onRefused: (notice: AttachFailureNotice & { filename: string }) => void;
  }): {
    door: AttachmentChipDoor;
    attempted: [string, string][];
  } {
    const attempted: [string, string][] = [];
    const storeDelete = useWorkspaceStore.getState().deleteAttachment;
    return {
      attempted,
      door: {
        threadId: THREAD_ID,
        async deleteAttachment(threadId, attachmentId) {
          attempted.push([threadId, attachmentId]);
          await storeDelete(threadId, attachmentId);
        },
        onDeleted: overrides.onDeleted,
        onRefused: overrides.onRefused,
        onRetry: () => {
          /* the retry door has its own tests */
        },
      },
    };
  }

  function refusalsTo(
    sink: (AttachFailureNotice & { filename: string })[],
  ): (notice: AttachFailureNotice & { filename: string }) => void {
    return (notice) => {
      sink.push(notice);
    };
  }

  function sliceIds(): string[] {
    return useWorkspaceStore.getState().attachments.map((a) => a.attachment_id);
  }

  function renderedNotice(
    failure: AttachFailureNotice & { filename: string },
  ): { props: Record<string, string>; text: string; testIds: string[] } {
    const nodes = nodesOf(
      AttachmentDeleteNotice({ failure, onDismiss: () => {} }),
    );
    const alerts = nodes.filter(
      (node) =>
        propsOf<{ "data-testid"?: string }>(node)["data-testid"] ===
        DELETE_NOTICE_TESTID,
    );
    expect(alerts).toHaveLength(1);
    return {
      props: propsOf<Record<string, string>>(alerts[0]),
      text: textOf(alerts[0]),
      testIds: nodes
        .map((node) => propsOf<{ "data-testid"?: string }>(node)["data-testid"])
        .filter((id): id is string => typeof id === "string"),
    };
  }

  it("clicking one row's × deletes exactly that attachment, then drops only that row", async () => {
    seedActiveThread(["att-1", "att-2"]);
    const { calls } = stubFetch(() =>
      jsonResponse({ ok: true, attachment_id: "att-2" }),
    );
    const onDeleted = vi.fn();
    const onRefused = vi.fn();
    const { door, attempted } = chipDoor({ onDeleted, onRefused });
    const rows = [attachment("att-1"), attachment("att-2")];

    await click(deleteControl(rows, door, "att-2"));

    // One call, naming the clicked row's own thread and id — never the
    // neighbour's, and never the whole list.
    expect(attempted).toEqual([[THREAD_ID, "att-2"]]);
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe("DELETE");
    expect(calls[0].url).toBe(deleteUrl("att-2"));
    // The state transition the store promises on a confirmed delete.
    expect(sliceIds()).toEqual(["att-1"]);
    expect(onDeleted).toHaveBeenCalledTimes(1);
    expect(onRefused).not.toHaveBeenCalled();
  });

  it("refuses to read a declined delete as a completion", async () => {
    seedActiveThread(["att-1"]);
    stubFetch(() =>
      jsonResponse({ error: "attachment_cleanup_failed" }, 500),
    );
    const onDeleted = vi.fn();
    const refusals: (AttachFailureNotice & { filename: string })[] = [];
    const { door } = chipDoor({
      onDeleted,
      onRefused: refusalsTo(refusals),
    });

    await click(deleteControl([attachment("att-1")], door, "att-1"));

    // The bytes are still owed, so the row stays and the refusal is shown.
    expect(sliceIds()).toEqual(["att-1"]);
    expect(onDeleted).not.toHaveBeenCalled();
    expect(refusals).toHaveLength(1);
    expect(refusals[0]).toEqual({
      kind: "attachment_cleanup_failed",
      message: expect.any(String),
      filename: "att-1.md",
    });
  });

  it.each([
    ["attachment_cleanup_failed", 500],
    ["unknown_locator", 400],
    ["invalid_locator", 400],
    ["attachment_too_large", 413],
  ])(
    "renders a %s delete refusal as its own typed line naming the row",
    async (kind, status) => {
      seedActiveThread(["att-1"]);
      stubFetch(() => jsonResponse({ error: kind }, status));
      const refusals: (AttachFailureNotice & { filename: string })[] = [];
      const { door } = chipDoor({
        onDeleted: () => {},
        onRefused: refusalsTo(refusals),
      });

      await click(deleteControl([attachment("att-1")], door, "att-1"));

      expect(refusals).toHaveLength(1);
      const notice = renderedNotice(refusals[0]);
      // The wire kind is on screen, not folded into a generic failure, and
      // the line says which attachment it is about.
      expect(notice.props["data-failure-kind"]).toBe(kind);
      expect(notice.props.role).toBe("alert");
      expect(notice.text).toContain(refusals[0].message);
      expect(notice.text).toContain("att-1.md");
      // A named failure never renders as a blank line.
      expect(notice.text.trim()).not.toBe("");
    },
  );

  it("keeps the four typed delete refusals distinct on screen", () => {
    const codes = [
      "attachment_cleanup_failed",
      "unknown_locator",
      "invalid_locator",
      "attachment_too_large",
    ];
    const lines = codes.map((code) =>
      renderedNotice({
        ...describeAttachFailure(new AttachFailedError(code, 500)),
        filename: "att-1.md",
      }),
    );

    // Four states the operator acts on differently are four different lines,
    // each still naming the wire kind it came from.
    expect(new Set(lines.map((line) => line.text)).size).toBe(codes.length);
    for (const [index, line] of lines.entries()) {
      expect(line.props["data-failure-kind"]).toBe(codes[index]);
      // The delete door's refusal owns its slot: it never renders into the
      // upload or send notice, so it cannot overwrite either.
      expect(line.testIds).not.toContain("attach-failure");
      expect(line.testIds).not.toContain("send-failure");
    }
  });
});
