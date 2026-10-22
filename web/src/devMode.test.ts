// The developer-mode switch (specs/demo-seed.md item 6, ADR 0100 decision
// 6). The helper is pure over an injected Storage so the node-env test
// suite can pin behavior without jsdom, and swallows storage failures —
// an unreadable flag just means the DEFAULT, which for this switch is
// OFF. Defaulting to off is the whole point: an unreadable storage must
// never be the reason an internal receipt surface reaches a user.

import { describe, expect, it } from "vitest";
import {
  DEVELOPER_MODE_KEY,
  readDeveloperMode,
  readDeveloperModeEnabled,
  writeDeveloperMode,
} from "./devMode";

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

describe("readDeveloperMode", () => {
  it("is OFF by default — nothing stored means the default view", () => {
    expect(readDeveloperMode(fakeStorage())).toBe(false);
  });

  it("is ON when the flag is set", () => {
    const storage = fakeStorage();
    storage.setItem(DEVELOPER_MODE_KEY, "1");
    expect(readDeveloperMode(storage)).toBe(true);
  });

  it("treats any other stored value as OFF, never as truthy", () => {
    // A sloppy truthy read would let "0" / "false" / "off" switch the
    // receipt surface ON, which is the one failure this default guards.
    for (const raw of ["0", "", "true", "on", "false", "yes"]) {
      const storage = fakeStorage();
      storage.setItem(DEVELOPER_MODE_KEY, raw);
      expect(readDeveloperMode(storage), raw).toBe(false);
    }
  });

  it("swallows a throwing getItem (storage unavailable)", () => {
    const storage = fakeStorage();
    storage.getItem = () => {
      throw new Error("denied");
    };
    expect(readDeveloperMode(storage)).toBe(false);
  });
});

describe("writeDeveloperMode", () => {
  it("stores 1 when enabled", () => {
    const storage = fakeStorage();
    writeDeveloperMode(true, storage);
    expect(storage.getItem(DEVELOPER_MODE_KEY)).toBe("1");
  });

  it("removes the key when disabled, so absence means the default", () => {
    const storage = fakeStorage();
    writeDeveloperMode(true, storage);
    writeDeveloperMode(false, storage);
    expect(storage.getItem(DEVELOPER_MODE_KEY)).toBeNull();
  });

  it("round-trips through the reader", () => {
    const storage = fakeStorage();
    writeDeveloperMode(true, storage);
    expect(readDeveloperMode(storage)).toBe(true);
    writeDeveloperMode(false, storage);
    expect(readDeveloperMode(storage)).toBe(false);
  });

  it("swallows a throwing setItem (storage unavailable)", () => {
    const storage = fakeStorage();
    storage.setItem = () => {
      throw new Error("denied");
    };
    expect(() => writeDeveloperMode(true, storage)).not.toThrow();
  });
});

describe("readDeveloperModeEnabled", () => {
  it("reads window.localStorage", () => {
    const storage = fakeStorage();
    writeDeveloperMode(true, storage);
    // Node env has no window; stub the global the helper reads so the
    // windowed path is pinned without jsdom.
    const originalWindow = globalThis.window;
    (globalThis as { window?: unknown }).window = { localStorage: storage };
    try {
      expect(readDeveloperModeEnabled()).toBe(true);
    } finally {
      (globalThis as { window?: unknown }).window = originalWindow;
    }
  });

  it("is OFF when window itself is inaccessible", () => {
    const originalWindow = globalThis.window;
    delete (globalThis as { window?: unknown }).window;
    try {
      expect(readDeveloperModeEnabled()).toBe(false);
    } finally {
      (globalThis as { window?: unknown }).window = originalWindow;
    }
  });

  it("is OFF when the storage access throws (privacy mode)", () => {
    const originalWindow = globalThis.window;
    (globalThis as { window?: unknown }).window = {
      get localStorage(): Storage {
        throw new Error("denied");
      },
    };
    try {
      expect(readDeveloperModeEnabled()).toBe(false);
    } finally {
      (globalThis as { window?: unknown }).window = originalWindow;
    }
  });
});
