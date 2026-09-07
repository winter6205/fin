// T2 (specs/workbench-sidebar-rail.md): the 会话 disclosure remembers
// its folded state for this session. The helper is pure over an
// injected Storage so the node-env test suite can pin behavior
// without jsdom, and swallows storage failures (privacy mode /
// unavailable storage) — an unreadable fold flag just means unfolded.

import { describe, expect, it } from "vitest";
import {
  SESSIONS_FOLDED_KEY,
  readSessionsFolded,
  writeSessionsFolded,
} from "./sessionKeys";

/** Minimal in-memory Storage stand-in. */
function fakeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (k) => (map.has(k) ? (map.get(k) as string) : null),
    key: (i) => Array.from(map.keys())[i] ?? null,
    removeItem: (k) => {
      map.delete(k);
    },
    setItem: (k, v) => {
      map.set(k, v);
    },
  };
}

describe("SESSIONS_FOLDED_KEY", () => {
  it("uses the session-scoped sidebar key", () => {
    expect(SESSIONS_FOLDED_KEY).toBe("fin.sidebar.sessions.folded");
  });
});

describe("readSessionsFolded", () => {
  it("defaults to unfolded when nothing is stored", () => {
    expect(readSessionsFolded(fakeStorage())).toBe(false);
  });

  it("reads folded when the key holds '1'", () => {
    const storage = fakeStorage();
    storage.setItem(SESSIONS_FOLDED_KEY, "1");
    expect(readSessionsFolded(storage)).toBe(true);
  });

  it("treats any other value as unfolded", () => {
    const storage = fakeStorage();
    storage.setItem(SESSIONS_FOLDED_KEY, "yes");
    expect(readSessionsFolded(storage)).toBe(false);
  });

  it("swallows a throwing getItem (storage unavailable)", () => {
    const storage = fakeStorage();
    storage.getItem = () => {
      throw new Error("denied");
    };
    expect(readSessionsFolded(storage)).toBe(false);
  });
});

describe("writeSessionsFolded", () => {
  it("writes '1' when folded", () => {
    const storage = fakeStorage();
    writeSessionsFolded(true, storage);
    expect(storage.getItem(SESSIONS_FOLDED_KEY)).toBe("1");
  });

  it("removes the key when unfolded", () => {
    const storage = fakeStorage();
    writeSessionsFolded(true, storage);
    writeSessionsFolded(false, storage);
    expect(storage.getItem(SESSIONS_FOLDED_KEY)).toBeNull();
  });

  it("swallows a throwing setItem (storage unavailable)", () => {
    const storage = fakeStorage();
    storage.setItem = () => {
      throw new Error("denied");
    };
    expect(() => writeSessionsFolded(true, storage)).not.toThrow();
  });
});
